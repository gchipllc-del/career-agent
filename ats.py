"""
ats.py — deterministic ATS scoring + gap analysis for the Career Agent.

"Beat the ATS every time" is a LOOP, not a smarter model: score the tailored
résumé against the JD, feed the gaps back, re-tailor, repeat until it clears the
target (or hits the attempt cap). The score weights what real ATS (Taleo /
Workday / Greenhouse) key on: required-skill keywords, overall JD coverage, and
role-title alignment.

TRUTHFUL CEILING (do not weaken): scoring rewards coverage of the candidate's
REAL experience, never fabrication. The pipeline's fabrication guard fails any
résumé that invents skills, so one that genuinely lacks a required keyword
plateaus below 100 and lands in human review with the gap named — by design. We
maximize the score *within the truth*; we do not keyword-stuff lies.
"""

import os
import re

import core

ATS_TARGET = int(os.getenv("ATS_TARGET", "75"))  # score (0-100) a résumé must clear

# JD lines signaling a hard requirement -> their keywords get the heaviest weight.
_REQ_LINE = re.compile(
    r"require|must|minimum|at least|\byears?\b|proficien|expert|strong|essential|qualif",
    re.I,
)


# The shared tokenizer (cached) — keeps this scorer's vocabulary identical to
# core.jd_coverage's gap analysis and avoids re-tokenizing the same JD ~12x/run.
_kw = core.keywords

# Words that TRIGGER the requirement-line detector but aren't skills themselves —
# without this filter they'd count as "required keywords" and distort the score's
# dominant (0.55) component.
_REQ_NOISE = frozenset((
    "least minimum minimums essential qualification qualifications proficiency "
    "proficient expert expertise require requires required requirement strong"
).split())


def _title_keywords(job_text):
    for ln in (job_text or "").splitlines():
        if ln.strip():
            return _kw(ln)
    return set()


def _required_keywords(job_text):
    req = set()
    for ln in (job_text or "").splitlines():
        if _REQ_LINE.search(ln):
            req |= _kw(ln)
    return req - _REQ_NOISE  # trigger words aren't skills


def score(job_text, resume_text):
    """Return an ATS score dict: composite 0-100 + the gaps that lower it."""
    jd, res = _kw(job_text), _kw(resume_text)
    if not jd:
        return {"score": 100, "coverage": 1.0, "required_coverage": 1.0, "missing": [],
                "missing_required": [], "title_match": True, "target": ATS_TARGET, "passes": True}
    coverage = len(jd & res) / len(jd)
    required = _required_keywords(job_text) & jd
    req_cov = (len(required & res) / len(required)) if required else 1.0
    title = _title_keywords(job_text) & jd
    title_match = (not title) or bool(title & res)
    # Required skills dominate, then overall coverage, then title alignment.
    composite = 0.55 * req_cov + 0.35 * coverage + 0.10 * (1.0 if title_match else 0.0)
    sc = round(composite * 100)
    return {
        "score": sc,
        "coverage": round(coverage, 3),
        "required_coverage": round(req_cov, 3),
        "missing": sorted(jd - res)[:20],
        "missing_required": sorted(required - res)[:15],
        "title_match": title_match,
        "target": ATS_TARGET,
        "passes": sc >= ATS_TARGET,
    }


def feedback(s):
    """Actionable, TRUTHFUL retry guidance derived from a score dict."""
    bits = [f"ATS score {s['score']}% (target {s['target']}%)."]
    if s.get("missing_required"):
        bits.append("Missing REQUIRED keywords: " + ", ".join(s["missing_required"]) + ".")
    elif s.get("missing"):
        bits.append("Missing JD keywords: " + ", ".join(s["missing"][:12]) + ".")
    if not s.get("title_match"):
        bits.append("Align the summary headline with the role's title.")
    bits.append("Weave these in ONLY where the master résumé's real experience supports "
                "them — do NOT invent skills, tools, or metrics.")
    return " ".join(bits)
