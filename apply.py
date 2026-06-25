"""
apply.py — assisted-apply foundation for the Career Agent.

COMPLIANCE BOUNDARY (do not weaken): we AUTO-FILL applications but NEVER
auto-submit. The optional driver (apply_autofill.py) fills the form and pauses
at the Submit button so the human reviews and submits. High-ban-risk boards
(LinkedIn, Indeed, …) are never automated — they get a prepared packet + apply
link instead. This is the lesson from auto-apply tools that got accounts banned.

Pure stdlib (sqlite3) so it imports with nothing installed. The browser driver
lives in apply_autofill.py behind a guarded Playwright import.
"""

import json
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlparse


# --- ATS detection ----------------------------------------------------------

# host substring -> ATS key (most specific first). Drives selector choice.
_ATS_HOSTS = [
    ("boards.greenhouse", "greenhouse"),
    ("greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("lever.co", "lever"),
    ("ashbyhq.com", "ashby"),
    ("myworkdayjobs.com", "workday"),
    ("smartrecruiters.com", "smartrecruiters"),
    ("workable.com", "workable"),
    ("breezy.hr", "breezy"),
]

# Boards that aggressively ban form automation -> never drive a browser there.
_NO_AUTOMATION = ["linkedin.com", "indeed.com", "glassdoor.com",
                  "ziprecruiter.com", "monster.com", "dice.com"]


def detect_ats(url):
    """Return an ATS key ('greenhouse'/'lever'/…), 'generic' for an unknown host,
    or None when there's no host at all."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    for needle, key in _ATS_HOSTS:
        if needle in host:
            return key
    return "generic"


def is_automatable(url):
    """(ok, reason). False for high-ban-risk boards -> packet fallback."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False, "No application URL provided."
    for banned in _NO_AUTOMATION:
        if banned in host:
            return False, (f"{banned} bans form automation (account-ban risk) — "
                           "prepared a packet + apply link for you to fill instead.")
    return True, ""


# --- fill planning ----------------------------------------------------------

def _name_parts(profile):
    full = (profile.get("full_name") or "").strip()
    bits = full.split()
    first = (profile.get("first_name") or (bits[0] if bits else "")).strip()
    last = (profile.get("last_name") or (bits[-1] if len(bits) > 1 else "")).strip()
    return (full or f"{first} {last}").strip(), first, last


def build_fill_plan(profile, ats):
    """Return [{key,label,value,type,selectors[]}] — the fields the driver fills.

    type is 'text' or 'upload'. Selectors are tried in order (ATS-specific first,
    then broad generic). Fields with no value are skipped. A form only has one of
    full-name vs first/last, so leaving both in the plan is safe — only matching
    selectors fire."""
    full, first, last = _name_parts(profile)
    # (key, label, value, type, generic_selectors, {ats: [extra_selectors]})
    rows = [
        ("first_name", "First name", first, "text",
         ['input[name*="first" i]', 'input[id*="first" i]', 'input[autocomplete="given-name"]'],
         {"greenhouse": ['#first_name']}),
        ("last_name", "Last name", last, "text",
         ['input[name*="last" i]', 'input[id*="last" i]', 'input[autocomplete="family-name"]'],
         {"greenhouse": ['#last_name']}),
        ("full_name", "Full name", full, "text",
         ['input[name="name" i]', 'input[id="name" i]', 'input[aria-label*="full name" i]'],
         {"lever": ['input[name="name"]'], "ashby": ['input[name="_systemfield_name"]']}),
        ("email", "Email", profile.get("email", ""), "text",
         ['input[type="email"]', 'input[name*="email" i]', 'input[id*="email" i]'],
         {"greenhouse": ['#email'], "lever": ['input[name="email"]']}),
        ("phone", "Phone", profile.get("phone", ""), "text",
         ['input[type="tel"]', 'input[name*="phone" i]', 'input[id*="phone" i]'],
         {"greenhouse": ['#phone'], "lever": ['input[name="phone"]']}),
        ("location", "Location", profile.get("location", ""), "text",
         ['input[name*="location" i]', 'input[id*="location" i]', 'input[aria-label*="location" i]'],
         {}),
        ("linkedin", "LinkedIn", profile.get("linkedin", ""), "text",
         ['input[name*="linkedin" i]', 'input[aria-label*="linkedin" i]'],
         {"lever": ['input[name="urls[LinkedIn]"]']}),
        ("portfolio", "GitHub / portfolio", profile.get("github", "") or profile.get("portfolio", ""), "text",
         ['input[name*="github" i]', 'input[name*="portfolio" i]', 'input[name*="website" i]'],
         {"lever": ['input[name="urls[GitHub]"]', 'input[name="urls[Portfolio]"]']}),
        ("resume", "Résumé upload", profile.get("resume_path", ""), "upload",
         ['input[type="file"]'],
         {"greenhouse": ['input#resume', 'input[type="file"]'], "lever": ['input[name="resume"]']}),
    ]
    plan = []
    for key, label, value, typ, generic, extra in rows:
        if not value:
            continue
        plan.append({"key": key, "label": label, "value": value, "type": typ,
                     "selectors": list(extra.get(ats, [])) + generic})
    return plan


def build_note(job_title, company, url, automatable, reason=""):
    """The human-facing note: which job + the link to finish & submit."""
    who = " — ".join([x for x in [job_title or "this role", company] if x])
    if automatable:
        return (f"✅ Application prepared for {who}. Your tailored résumé is saved and "
                f"the form will be filled and paused at the Submit button — review it, "
                f"then submit yourself:\n{url}")
    return (f"📋 Application prepared for {who}. {reason}\n"
            f"Your tailored résumé is saved; open the link, attach it, and submit:\n{url}")


# --- store ------------------------------------------------------------------

class ApplyStore:
    """SQLite-backed profile + application tracker. Own connection/lock so it's
    independent of the runs DB. Single-user low concurrency -> a busy_timeout
    covers the rare cross-connection contention."""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=4000")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS profile ("
                "  id INTEGER PRIMARY KEY CHECK (id=1),"
                "  data TEXT NOT NULL)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS applications ("
                "  id TEXT PRIMARY KEY, run_id TEXT,"
                "  job_title TEXT, company TEXT, url TEXT, source TEXT, ats TEXT,"
                "  resume_path TEXT, status TEXT NOT NULL, note TEXT,"
                "  created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            self._conn.commit()

    def get_profile(self):
        with self._lock:
            row = self._conn.execute("SELECT data FROM profile WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def set_profile(self, data):
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO profile(id, data) VALUES (1, ?)",
                               (json.dumps(data),))
            self._conn.commit()
        return data

    def _row(self, row, cols):
        return dict(zip(cols, row)) if row else None

    def add_application(self, app):
        now = time.time()
        app_id = app.get("id") or uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO applications"
                "(id, run_id, job_title, company, url, source, ats, resume_path, status, note, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (app_id, app.get("run_id"), app.get("job_title"), app.get("company"),
                 app.get("url"), app.get("source"), app.get("ats"), app.get("resume_path"),
                 app.get("status", "prepared"), app.get("note"), now, now))
            self._conn.commit()
        return self.get_application(app_id)

    def get_application(self, app_id):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM applications WHERE id=?", (app_id,))
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
        return self._row(row, cols)

    def list_applications(self):
        with self._lock:
            cur = self._conn.execute("SELECT * FROM applications ORDER BY created_at DESC")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def set_status(self, app_id, status):
        with self._lock:
            self._conn.execute("UPDATE applications SET status=?, updated_at=? WHERE id=?",
                               (status, time.time(), app_id))
            self._conn.commit()
        return self.get_application(app_id)
