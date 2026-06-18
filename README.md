# Autonomous Career Agent

Tailors a master résumé to a job posting under a **zero-fabrication** guarantee,
with prompt-injection defense and a human-approval gate before anything is submitted.

```
scrape ─▶ sanitize ─▶ tailor ─▶ validate ─▶ [human approval] ─▶ submit
                          ▲          │
                          └──fail────┘  (max 3 attempts, then manual review)
```

## Run the dashboard (no installs)

```bash
python3 server.py
# open http://127.0.0.1:8000
```

Runs on stock Python 3.9 — stdlib only. In this **mock mode** the LLM and the
scraper are stubbed, but the two security-critical layers are fully live:

- **Injection scan + fencing** — try *“Load injection example ⚠”* in the UI.
- **Deterministic fabrication check** — invented numbers/dates are caught even
  without an LLM.

The pipeline stepper shows **real progress**: each run starts in the background
and streams genuine per-stage events (`scrape → sanitize → tailor → validate`)
to the dashboard over Server-Sent Events — not a canned animation. (Opening the
page as a `file://` with no server falls back to a local in-browser simulation.)

## Switch on the real engine

The engine is provider-agnostic — pick one with `LLM_PROVIDER`. Three headline ways:

### A) Personal / highest quality — Claude Opus (paid)

Settings are auto-loaded from a local `.env` (gitignored), so you configure once:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt langchain-anthropic
# edit .env -> set ANTHROPIC_API_KEY=sk-ant-...  (LLM_PROVIDER=anthropic is preset)
.venv/bin/python server.py               # badge: "langgraph · anthropic claude-opus-4-8"
```

(Env vars still override `.env`, e.g. `LLM_PROVIDER=groq .venv/bin/python server.py`.)
Uses the **native Anthropic client** (`langchain-anthropic`, wrapping the official
`anthropic` SDK) — not an OpenAI shim. Model defaults to `claude-opus-4-8`. Note:
Opus rejects `temperature`/`top_p`/`top_k`, so the engine never sends them.

### B) Anyone, no API key — a local model on your computer

```bash
# Install the Ollama app, then:
ollama pull llama3.2
pip install -r requirements.txt
export LLM_PROVIDER=ollama
python3 server.py                        # badge: "langgraph · ollama llama3.2"
```

Runs fully offline, $0, no signup. (And with **nothing** set at all, the app stays
in zero-install **mock mode** that literally anyone can run.)

### C) Free hosted tier — Groq (no credit card)

```bash
pip install -r requirements.txt
export LLM_PROVIDER=groq
export OPENAI_API_KEY=gsk_...            # free key at console.groq.com/keys
python3 server.py
```

No Firecrawl/OpenAI bill needed either: **paste the job description** instead of a URL.

### Provider matrix

| `LLM_PROVIDER` | Cost | Key env var | Notes |
|---|---|---|---|
| `anthropic` | Paid | `ANTHROPIC_API_KEY` | Claude Opus — best quality. Native Anthropic client. |
| `ollama` | Free | *(none)* | Local model on your machine. Install Ollama + `ollama pull`. |
| `groq` *(default)* | Free | `OPENAI_API_KEY` | 30 RPM · ~100K tok/day. [console.groq.com/keys](https://console.groq.com/keys) |
| `openrouter` | Free | `OPENAI_API_KEY` | 20 RPM · 50 req/day. Pin a tool-calling `:free` model via `LLM_MODEL`. |
| `cerebras` | Free | `OPENAI_API_KEY` | ~1M tok/day, 8K context cap. `LLM_MODEL=llama-3.1-8b`. |
| `openai` | Paid | `OPENAI_API_KEY` | Standard OpenAI. |

Override the model with `LLM_MODEL`. See `.env.example`.

**Structured-output gotcha:** the validator uses default tool-calling
`with_structured_output()` — keep it that way. Do **not** add
`method="json_schema"`/`strict` on Groq's `gpt-oss` models (broken,
[LangChain #34155](https://github.com/langchain-ai/langchain/issues/34155)), and
don't route Gemini through the OpenAI-compat endpoint (400s on the validator).

The server auto-detects: if LangGraph imports, it drives the graph
(`career_agent.py`); otherwise it falls back to the stdlib pipeline (`core.py`).

## Files

| File | Role |
|------|------|
| `core.py` | Dependency-free security + validation logic (shared) and the mock pipeline |
| `career_agent.py` | Hardened LangGraph engine (HITL interrupt, retry breaker, injection node) |
| `server.py` | Zero-dependency stdlib web server / JSON API |
| `dashboard.html` | Single-file UI |

## Defenses

1. **Input fencing** — scraped text is wrapped in a fence with an unguessable
   per-run sentinel; the tailoring prompt treats fenced content as data, never
   instructions. Injected text can't forge the closing marker.
2. **Heuristic injection scan** — advisory signatures (override / role-hijack /
   exfiltration / fabrication-push) surfaced to the human.
3. **Grounded validation** — both résumés go to an LLM judge, backstopped by a
   deterministic number/date diff against the master.
4. **Human-in-the-loop** — submission is blocked until a person approves; failed
   validation escalates to manual review instead of looping forever.

## Persistence

Run state survives a restart out of the box: `server.py` stores run status +
results in a SQLite file (`runs.sqlite`, override with `RUNS_DB`, or
`RUNS_DB=:memory:` for ephemeral) — pure stdlib, no dependency. On the LangGraph
path, the graph checkpoint also persists when `langgraph-checkpoint-sqlite` is
installed (`CHECKPOINT_DB`), so a pending human approval resumes after a crash.

## Tests

```bash
python3 -m unittest discover -s tests      # zero installs (stdlib unittest)
# or:  pytest tests/
```

Covers the injection scan (incl. Unicode-obfuscation bypasses), the fence,
fabrication + canary checks, the run/approve/reject HTTP lifecycle, and
run-state persistence across a simulated restart.

A **red-team harness** (`tests/test_redteam.py` over `tests/fixtures/injection_corpus.json`)
adds a labeled injection corpus and gates three properties: direct injections
stay caught (regression guard), benign postings never flag (precision guard), and
the fence structurally neutralizes **every** injection even when the advisory
regex misses it. Heavier out-of-band audits (promptfoo, garak's
`LatentInjectionResume`) are documented in `tests/redteam_external.md` — dev/CI
only, never runtime.
