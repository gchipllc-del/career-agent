"""
server.py — zero-dependency backend for the Career Agent dashboard.

Runs on stock Python 3.9 with NOTHING installed (uses stdlib http.server).
If LangGraph + an OPENAI_API_KEY are present it drives the real graph; otherwise
it falls back to core.run_pipeline so the dashboard is always demoable.

Run:  python3 server.py     then open  http://127.0.0.1:8000
"""

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import localenv
localenv.load()  # apply a personal .env (LLM_PROVIDER, API keys) before imports read it

import core
import jobs_search
import resume_io

# Prefer the real LangGraph engine when its deps are importable.
try:
    import career_agent as ca
    _HAS_GRAPH = True
except Exception:
    ca = None
    _HAS_GRAPH = False

ENGINE = "langgraph" if _HAS_GRAPH else "stdlib"
MOCK = (ca.MOCK_MODE if _HAS_GRAPH else True)
PROVIDER = (getattr(ca, "ACTIVE_PROVIDER", None) if (_HAS_GRAPH and not MOCK) else None)
MODEL = (getattr(ca, "ACTIVE_MODEL", None) if (_HAS_GRAPH and not MOCK) else None)
DASHBOARD = Path(__file__).with_name("dashboard.html")


class RunStore:
    """Thread-safe, SQLite-backed store for run status + public state.

    Replaces the old in-memory dict so a submitted/approved run survives a
    process restart. The server is threaded (ThreadingHTTPServer), so a single
    connection is opened with check_same_thread=False and every access is
    serialized through a lock. Pure stdlib — no new dependency."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "  run_id     TEXT PRIMARY KEY,"
                "  status     TEXT NOT NULL,"
                "  state      TEXT NOT NULL,"
                "  created_at REAL NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "  key        TEXT PRIMARY KEY,"
                "  payload    TEXT NOT NULL,"
                "  fetched_at REAL NOT NULL)"
            )
            self._conn.commit()

    def create(self, run_id, status, state):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, status, state, created_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, status, json.dumps(state), time.time()),
            )
            self._conn.commit()

    def get(self, run_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT status, state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else {"status": row[0], "state": json.loads(row[1])}

    def set_status(self, run_id, status):
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id)
            )
            self._conn.commit()

    def get_cache(self, key, max_age):
        """Return the cached payload (parsed) if newer than max_age seconds, else None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, fetched_at FROM cache WHERE key = ?", (key,)
            ).fetchone()
        if row is None or (time.time() - row[1]) > max_age:
            return None
        return json.loads(row[0])

    def put_cache(self, key, payload, now):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache(key, payload, fetched_at) VALUES (?, ?, ?)",
                (key, json.dumps(payload), now),
            )
            self._conn.commit()


# Default to a file so runs persist; set RUNS_DB=":memory:" for an ephemeral store.
RUNS = RunStore(os.getenv("RUNS_DB", "runs.sqlite"))


def _public(state):
    return {
        "engine": state.get("engine", ENGINE),
        "injection_flags": state.get("injection_flags", []),
        "validation_status": state.get("validation_status", ""),
        "audit_notes": state.get("audit_notes", ""),
        "tailored_resume": state.get("tailored_resume", ""),
        "coverage": state.get("coverage") or {},
        "retry_count": state.get("retry_count", 0),
        "job_chars": len((state.get("scraped_job_spec") or "")),
    }


def engine_run_events(run_id, master, job_text, job_url):
    """Yield progress events for a run (stage events then a final
    {'event':'done','state':...}). Dispatches to the LangGraph engine or the
    stdlib pipeline; run_id is the graph thread_id."""
    if _HAS_GRAPH:
        yield from ca.run_to_gate_events(run_id, master, job_text=job_text, job_url=job_url)
    else:
        yield from core.run_pipeline_events(master, job_text)


def engine_submit(run_id):
    if _HAS_GRAPH:
        ca.resume(run_id)  # traverse past the interrupt to END


class ProgressChannel:
    """In-process, replayable event log for one run's live progress. The worker
    thread push()es events; the SSE handler stream()s them, replaying from the
    start so a late client still sees the whole sequence. Ephemeral — the final
    state is persisted separately in RunStore."""

    def __init__(self):
        self._events = []
        self._done = False
        self._cond = threading.Condition()

    def push(self, ev):
        with self._cond:
            self._events.append(ev)
            if ev.get("event") in ("done", "error"):
                self._done = True
            self._cond.notify_all()

    def stream(self):
        i = 0
        while True:
            with self._cond:
                while i >= len(self._events):
                    if self._done:
                        return
                    self._cond.wait(timeout=2.0)
                ev = self._events[i]
                i += 1
            yield ev
            if ev.get("event") in ("done", "error"):
                return


PROGRESS = {}                       # run_id -> ProgressChannel (live, ephemeral)
PROGRESS_LOCK = threading.Lock()


def _run_worker(run_id, channel, master, job_text, job_url):
    """Background thread: run the pipeline, push progress to the channel, and
    persist the final public state to RunStore BEFORE signalling 'done' (so an
    immediate approve always finds the run)."""
    try:
        for ev in engine_run_events(run_id, master, job_text, job_url):
            if ev.get("event") == "done":
                pub = _public(ev["state"])
                status = ("awaiting_approval"
                          if pub["validation_status"] == "PASSED" else "needs_review")
                RUNS.create(run_id, status, pub)
                channel.push({"event": "done", "run_id": run_id,
                              "status": status, "state": pub})
            else:
                channel.push(ev)
    except Exception as exc:
        channel.push({"event": "error", "message": f"{type(exc).__name__}: {exc}"})


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No CORS headers: the dashboard is served same-origin by this server,
        # so cross-origin access is intentionally not granted (smaller surface).
        self.end_headers()
        self.wfile.write(body)

    def _send_download(self, data, filename, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 16 * 1024 * 1024:
            raise ValueError("request body too large")
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def log_message(self, *args):
        pass  # quiet

    def do_OPTIONS(self):
        self._send(204, b"", ctype="text/plain")

    def _serve_sse(self, events):
        """Stream an iterable of event dicts as Server-Sent Events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for ev in events:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                return self._send(200, DASHBOARD.read_bytes(), ctype="text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(500, {"error": "dashboard.html not found"})
        if self.path == "/api/health":
            return self._send(200, {"engine": ENGINE, "mock_mode": MOCK,
                                    "provider": PROVIDER, "model": MODEL})
        if self.path.startswith("/api/runs/") and self.path.endswith("/events"):
            run_id = self.path[len("/api/runs/"):-len("/events")]
            with PROGRESS_LOCK:
                channel = PROGRESS.get(run_id)
            if channel is not None:
                return self._serve_sse(channel.stream())
            run = RUNS.get(run_id)  # finished/pruned (e.g. after restart) -> one-shot
            if run is not None:
                return self._serve_sse(iter([{"event": "done", "run_id": run_id, **run}]))
            return self._send(404, {"error": "unknown run"})
        if self.path.startswith("/api/runs/"):
            run_id = self.path.rsplit("/", 1)[-1]
            run = RUNS.get(run_id)
            if not run:
                return self._send(404, {"error": "unknown run"})
            return self._send(200, {"run_id": run_id, **run})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/runs":
                data = self._body()
                master = (data.get("master_resume") or "").strip()
                if not master:
                    return self._send(400, {"error": "master_resume is required"})
                run_id = uuid.uuid4().hex[:12]
                channel = ProgressChannel()
                with PROGRESS_LOCK:
                    PROGRESS[run_id] = channel
                threading.Thread(
                    target=_run_worker,
                    args=(run_id, channel, master,
                          (data.get("job_text") or ""),
                          (data.get("job_url") or "").strip()),
                    daemon=True,
                ).start()
                # Runs in the background; client streams progress from /events.
                return self._send(202, {"run_id": run_id, "status": "running"})

            if self.path == "/api/search":
                data = self._body()
                query = (data.get("query") or "").strip()
                if not query:
                    return self._send(400, {"error": "query is required"})
                # General feeds are query-independent -> cache once (6h), shared
                # across queries (respects Remotive's ~4/day cap).
                feed = RUNS.get_cache("jobfeed", max_age=6 * 3600)
                feed_cached = feed is not None
                if feed is None:
                    feed = jobs_search.search_all(query, adapters=jobs_search.FEED_ADAPTERS)
                    RUNS.put_cache("jobfeed", feed, time.time())
                # Keyword search (Adzuna) IS query-specific -> cache per query.
                qkey = "kw:" + hashlib.sha1(query.lower().encode()).hexdigest()[:16]
                kw = RUNS.get_cache(qkey, max_age=6 * 3600)
                if kw is None:
                    kw = jobs_search.search_all(query, adapters=jobs_search.KEYWORD_ADAPTERS)
                    RUNS.put_cache(qkey, kw, time.time())
                merged = jobs_search.dedupe(feed.get("results", []) + kw.get("results", []))
                # Expand the query (LLM when a real provider is live) and rank with
                # the distinctive-term gate so generic words don't float junk up.
                llm = ca.LLM if (_HAS_GRAPH and not ca.MOCK_MODE) else None
                terms = jobs_search.expand_query(query, llm=llm)
                ranked = jobs_search.rank_matches(terms, merged, min_score=0.03, gate=True)[:50]
                sources_ok = sorted(set(feed.get("sources_ok", []) + kw.get("sources_ok", [])))
                sources_skipped = sorted(set(feed.get("sources_skipped", []) + kw.get("sources_skipped", [])))
                return self._send(200, {
                    "results": ranked, "sources_ok": sources_ok, "sources_skipped": sources_skipped,
                    "cached": feed_cached, "count": len(ranked),
                    "weak": bool(ranked) and ranked[0].get("score", 0) < 0.08,
                    "keyword_search": "adzuna" in sources_ok,
                })

            if self.path == "/api/parse":
                data = self._body()
                try:
                    raw = base64.b64decode(data.get("content_b64") or "")
                except Exception:
                    return self._send(400, {"error": "invalid file data"})
                try:
                    text = resume_io.parse_resume(data.get("filename", ""), raw)
                except resume_io.ParseError as exc:
                    return self._send(400, {"error": str(exc)})
                return self._send(200, {"text": text, "chars": len(text)})

            if self.path == "/api/export":
                data = self._body()
                text = data.get("text") or ""
                fmt = (data.get("format") or "docx").lower()
                base = (data.get("filename") or "tailored_resume").rsplit(".", 1)[0]
                if fmt == "txt":
                    return self._send_download(resume_io.to_txt(text), base + ".txt",
                                               "text/plain; charset=utf-8")
                if fmt == "docx":
                    return self._send_download(
                        resume_io.to_docx(text), base + ".docx",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                return self._send(400, {"error": "unknown export format"})

            parts = self.path.strip("/").split("/")  # api / runs / {id} / {action}
            if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                run_id, action = parts[2], parts[3]
                run = RUNS.get(run_id)
                if not run:
                    return self._send(404, {"error": "unknown run"})
                if action == "approve":
                    if run["status"] != "awaiting_approval":
                        return self._send(409, {"error": "run is not awaiting approval"})
                    engine_submit(run_id)  # before persisting: if it raises, status stays
                    RUNS.set_status(run_id, "submitted")
                    run["status"] = "submitted"
                    return self._send(200, {"run_id": run_id, **run})
                if action == "reject":
                    RUNS.set_status(run_id, "rejected")
                    run["status"] = "rejected"
                    return self._send(200, {"run_id": run_id, **run})
            return self._send(404, {"error": "not found"})
        except Exception as exc:  # never 500 silently
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    host, port = "127.0.0.1", int(os.getenv("PORT", "8000"))
    print(f"Career Agent dashboard  ->  http://{host}:{port}")
    print(f"  engine: {ENGINE}   mock_mode: {MOCK}")
    print(f"  runs db: {os.getenv('RUNS_DB', 'runs.sqlite')} (survives restart)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
