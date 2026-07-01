"""
apply_autofill.py — local browser autofill (optional, Playwright).

Drives a REAL headed browser to a job's application page, fills the form from
your profile, uploads your tailored résumé, and STOPS at the Submit button. It
NEVER clicks Submit — you review and submit yourself.

Install:  pip install playwright   &&   playwright install chromium
Run directly:  python apply_autofill.py <apply_url> '<profile_json>'
The server launches this as a SUBPROCESS so the browser opens on your desktop and
lives independently of the HTTP request.

Import is guarded so the rest of the app runs with nothing installed.
"""

import json
import re
import sys

import apply

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False


class AutofillUnavailable(RuntimeError):
    """Raised when autofill can't run (no Playwright, or a ban-risk board)."""


def preflight(url):
    """Cheap checks the server can run before spawning a browser.
    Returns (ok, reason). ok=False -> caller should fall back to a packet."""
    if not HAVE_PLAYWRIGHT:
        return False, ("Playwright is not installed. Run:\n"
                       "  pip install playwright && playwright install chromium")
    return apply.is_automatable(url)


def _find_by_label(page, pattern):
    """Resolve an input via its <label for=...> text — the only reliable handle on
    per-job custom questions whose input ids are random per posting."""
    try:
        rx = re.compile(pattern, re.I)
        for lb in page.query_selector_all("label[for]"):
            try:
                if not rx.search((lb.inner_text() or "").strip()):
                    continue
                fid = lb.get_attribute("for") or ""
                # ids here are ATS-generated (e.g. question_123); quote defensively.
                if fid and re.fullmatch(r"[A-Za-z0-9_\-:.]+", fid):
                    el = page.query_selector(f'[id="{fid}"]')
                    if el:
                        return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def _commit_combobox(page, el):
    """Autocomplete listboxes (e.g. Greenhouse's country/location combobox) don't
    accept a bare fill — the value must be chosen from the popup. After typing,
    pick the first suggestion. No-op for plain text inputs."""
    try:
        if (el.get_attribute("role") == "combobox"
                or el.get_attribute("aria-autocomplete") == "list"):
            page.wait_for_timeout(800)  # let the suggestion list populate
            el.press("ArrowDown")
            el.press("Enter")
    except Exception:
        pass


def fill_application(url, profile, ats=None):
    """Open url, fill the form per build_fill_plan, upload the résumé, and leave
    the browser open at the Submit step until the user closes it. Returns a
    summary dict. Blocks while the window is open (run in a subprocess)."""
    if not HAVE_PLAYWRIGHT:
        raise AutofillUnavailable(
            "Playwright not installed: pip install playwright && playwright install chromium")
    ok, reason = apply.is_automatable(url)
    if not ok:
        raise AutofillUnavailable(reason)

    ats = ats or apply.detect_ats(url)
    plan = apply.build_fill_plan(profile, ats)
    filled, skipped = [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)

        # Some boards show an "Apply" button before the form -> click it if present.
        for sel in ('a:has-text("Apply")', 'button:has-text("Apply")',
                    'a:has-text("Apply for this job")'):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    el.click()
                    page.wait_for_timeout(1500)
                    break
            except Exception:
                pass

        for field in plan:
            el = None
            for sel in field["selectors"]:
                try:
                    el = page.query_selector(sel)
                except Exception:
                    el = None
                if el:
                    break
            # Last resort: resolve per-job custom questions (random input ids like
            # Greenhouse question_11163025008) via their <label for=...> text.
            if el is None and field.get("label_match"):
                el = _find_by_label(page, field["label_match"])
            if el is None:
                skipped.append(field["key"])
                continue
            try:
                if field["type"] == "upload":
                    el.set_input_files(field["value"])  # works on hidden inputs; fires change
                else:
                    el.fill(str(field["value"]))  # Playwright fill fires input+change (React-safe)
                    _commit_combobox(page, el)
                filled.append(field["key"])
            except Exception:
                skipped.append(field["key"])

        # HARD STOP: never submit. Surface a banner and hold the window open until
        # the user closes it (they review + click Submit themselves).
        try:
            page.evaluate(
                "() => { const b=document.createElement('div');"
                "b.textContent='✓ Auto-filled by Career Agent — REVIEW and click Submit yourself. (Nothing was submitted.)';"
                "b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;"
                "background:#0b8043;color:#fff;font:600 14px system-ui;padding:10px;text-align:center';"
                "document.body.appendChild(b); }")
        except Exception:
            pass

        print(json.dumps({"ok": True, "ats": ats, "filled": filled, "skipped": skipped}))
        try:
            page.wait_for_event("close", timeout=0)  # until the user closes the tab
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass

    return {"ok": True, "ats": ats, "filled": filled, "skipped": skipped}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python apply_autofill.py <apply_url> '<profile_json>'", file=sys.stderr)
        sys.exit(2)
    _url = sys.argv[1]
    _profile = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        fill_application(_url, _profile)
    except AutofillUnavailable as e:
        print(f"AUTOFILL UNAVAILABLE: {e}", file=sys.stderr)
        sys.exit(1)
