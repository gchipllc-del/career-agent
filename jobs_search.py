"""Job discovery — fan out across free, ToS-permissible job APIs, normalize to a
canonical record, merge/dedupe, and rank against a free-text query (stdlib
TF-IDF). No new dependencies (urllib + concurrent.futures + math).

Every source here is free and permits programmatic use WITH attribution, which
each record carries and the UI renders. Aggregated postings are untrusted text:
they flow through core.build_safe_spec()/scan_injection() when tailored, exactly
like a scraped posting. Field mappings were taken from the live API responses.
"""

import base64
import concurrent.futures
import html
import json
import math
import os
import re
import urllib.parse
import urllib.request
from collections import Counter

UA = "Mozilla/5.0 (CareerAgent; local job search)"
TIMEOUT = 15
_TAG = re.compile(r"<[^>]+>")


def _get(url, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _text(s):
    """HTML -> plain-ish text (entities unescaped, tags stripped)."""
    return _TAG.sub(" ", html.unescape(s or "")).strip()


def _rec(source, sid, title, company, location, etype, remote, url, desc, posted, attribution):
    return {
        "source": source, "source_job_id": str(sid or ""),
        "title": (title or "").strip(), "company": (company or "").strip(),
        "location": (location or "").strip(), "employment_type": (etype or "").strip(),
        "remote": bool(remote), "url": (url or "").strip(),
        "description": _text(desc)[:6000], "posted_at": str(posted or ""),
        "attribution": attribution,
    }


# --- source adapters (signature: fetch(query) -> list[record]) --------------

def himalayas(query, limit=100):
    d = _get(f"https://himalayas.app/jobs/api?limit={limit}")
    out = []
    for j in d.get("jobs", []):
        loc = j.get("locationRestrictions") or []
        out.append(_rec(
            "Himalayas", j.get("guid"), j.get("title"), j.get("companyName"),
            ", ".join(loc) if isinstance(loc, list) else loc,
            j.get("employmentType"), True, j.get("applicationLink"),
            j.get("description") or j.get("excerpt"), j.get("pubDate"),
            {"name": "Himalayas", "link": "https://himalayas.app"}))
    return out


def remotive(query, limit=100):
    d = _get(f"https://remotive.com/api/remote-jobs?limit={limit}")
    out = []
    for j in d.get("jobs", []):
        out.append(_rec(
            "Remotive", j.get("id"), j.get("title"), j.get("company_name"),
            j.get("candidate_required_location"), j.get("job_type"), True, j.get("url"),
            j.get("description"), j.get("publication_date"),
            {"name": "Remotive", "link": j.get("url") or "https://remotive.com"}))
    return out


def remoteok(query):
    d = _get("https://remoteok.com/api")
    out = []
    for j in (d[1:] if isinstance(d, list) else []):  # index 0 is the legal/attribution object
        if not isinstance(j, dict) or not j.get("position"):
            continue
        tags = [str(t).lower() for t in (j.get("tags") or [])]
        etype = "contract" if any("contract" in t for t in tags) else ""
        out.append(_rec(
            "RemoteOK", j.get("id"), j.get("position"), j.get("company"),
            j.get("location"), etype, True, j.get("apply_url") or j.get("url"),
            j.get("description"), j.get("date"),
            {"name": "Remote OK", "link": j.get("url") or "https://remoteok.com", "direct": True}))
    return out


def _lever(company):
    d = _get(f"https://api.lever.co/v0/postings/{company}?mode=json")
    out = []
    for j in (d if isinstance(d, list) else [])[:60]:
        cats = j.get("categories") or {}
        out.append(_rec(
            "Lever", j.get("id"), j.get("text"), company.title(),
            cats.get("location"), cats.get("commitment"),
            (j.get("workplaceType") == "remote"), j.get("hostedUrl") or j.get("applyUrl"),
            j.get("descriptionPlain") or j.get("description"), j.get("createdAt"),
            {"name": f"{company.title()} (Lever)", "link": j.get("hostedUrl") or ""}))
    return out


def _greenhouse(company):
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true")
    out = []
    for j in (d.get("jobs", []))[:60]:
        loc = (j.get("location") or {}).get("name") or ""
        out.append(_rec(
            "Greenhouse", j.get("id"), j.get("title"), j.get("company_name") or company.title(),
            loc, "", ("remote" in loc.lower()), j.get("absolute_url"),
            j.get("content"), j.get("updated_at"),
            {"name": f"{company.title()} (Greenhouse)", "link": j.get("absolute_url") or ""}))
    return out


def company_boards(query):
    """Greenhouse/Lever per-company boards from $COMPANY_BOARDS
    ("greenhouse:stripe,lever:leverdemo"). Each board is fetched independently;
    a bad token never sinks the others."""
    spec = os.getenv("COMPANY_BOARDS", "greenhouse:stripe,lever:leverdemo")
    out = []
    for entry in spec.split(","):
        entry = entry.strip()
        if ":" not in entry:
            continue
        kind, _, co = entry.partition(":")
        kind, co = kind.strip().lower(), co.strip()
        try:
            out += _greenhouse(co) if kind == "greenhouse" else _lever(co) if kind == "lever" else []
        except Exception:
            continue
    return out


def adzuna(query):
    """Real server-side keyword search across all sectors (so niche roles like
    'SOC analyst' actually appear). Keyed — self-skips when ADZUNA_APP_ID /
    ADZUNA_APP_KEY are absent, mirroring the LLM MOCK fallback. Free key at
    developer.adzuna.com. (ToS: intended for short/personal use, attribution
    required — we render 'source: Adzuna' + redirect to apply.)"""
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    country = os.getenv("ADZUNA_COUNTRY", "us")
    url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
           f"?app_id={app_id}&app_key={app_key}"
           f"&what={urllib.parse.quote(query)}&results_per_page=50&content-type=application/json")
    d = _get(url)
    out = []
    for j in d.get("results", []):
        loc = (j.get("location") or {}).get("display_name") or ""
        out.append(_rec(
            "Adzuna", j.get("id"), j.get("title"), (j.get("company") or {}).get("display_name"),
            loc, j.get("contract_type") or j.get("contract_time") or "",
            "remote" in (loc + " " + (j.get("title") or "")).lower(),
            j.get("redirect_url"), j.get("description"), j.get("created"),
            {"name": "Adzuna", "link": j.get("redirect_url") or "https://www.adzuna.com"}))
    return out


def jsearch(query):
    """Google-for-Jobs aggregator via JSearch on RapidAPI — covers LinkedIn,
    Indeed, Glassdoor, ZipRecruiter and other publishers in one keyword search
    (the legitimate way to reach those: you consume JSearch's aggregated feed).
    Keyed — self-skips without RAPIDAPI_KEY. Free tier ~200 req/month, so results
    are cached per query for 6h. Attribution names the original publisher."""
    key = os.getenv("RAPIDAPI_KEY")
    if not key:
        return []
    url = ("https://jsearch.p.rapidapi.com/search"
           f"?query={urllib.parse.quote(query)}&page=1&num_pages=1")
    d = _get(url, headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    out = []
    for j in (d.get("data", []) if isinstance(d, dict) else []):
        loc = ", ".join(x for x in [j.get("job_city"), j.get("job_state"), j.get("job_country")] if x)
        pub = j.get("job_publisher") or "Google Jobs"
        out.append(_rec(
            "Google Jobs", j.get("job_id"), j.get("job_title"), j.get("employer_name"),
            loc, j.get("job_employment_type"), j.get("job_is_remote"),
            j.get("job_apply_link"), j.get("job_description"), j.get("job_posted_at_datetime_utc"),
            {"name": f"via {pub}", "link": j.get("job_apply_link") or ""}))
    return out


def reed(query):
    """Reed.co.uk official Jobseeker API — sanctioned key-based access (NOT
    scraping), ~1000 req/day. Self-skips without REED_API_KEY. HTTP Basic auth:
    the key is the username, password blank. UK-heavy; complements Adzuna."""
    key = os.getenv("REED_API_KEY")
    if not key:
        return []
    url = ("https://www.reed.co.uk/api/1.0/search"
           f"?keywords={urllib.parse.quote(query)}&resultsToTake=50")
    auth = base64.b64encode(f"{key}:".encode()).decode()
    d = _get(url, headers={"Authorization": f"Basic {auth}"})
    out = []
    for j in d.get("results", []):
        loc = j.get("locationName") or ""
        out.append(_rec(
            "Reed", j.get("jobId"), j.get("jobTitle"), j.get("employerName"),
            loc, j.get("contractType") or "",
            "remote" in (loc + " " + (j.get("jobTitle") or "")).lower(),
            j.get("jobUrl"), j.get("jobDescription"), j.get("date"),
            {"name": "Reed", "link": j.get("jobUrl") or "https://www.reed.co.uk"}))
    return out


def usajobs(query):
    """USAJOBS.gov official REST API (US GSA — free, sanctioned, cleanest ToS).
    Self-skips unless USAJOBS_API_KEY + USAJOBS_EMAIL are set."""
    key, email = os.getenv("USAJOBS_API_KEY"), os.getenv("USAJOBS_EMAIL")
    if not (key and email):
        return []
    url = ("https://data.usajobs.gov/api/search"
           f"?Keyword={urllib.parse.quote(query)}&ResultsPerPage=50")
    d = _get(url, headers={"Authorization-Key": key, "User-Agent": email,
                           "Host": "data.usajobs.gov"})
    out = []
    for item in (d.get("SearchResult", {}) or {}).get("SearchResultItems", []):
        m = item.get("MatchedObjectDescriptor", {}) or {}
        loc = ", ".join(l.get("LocationName", "") for l in (m.get("PositionLocation") or []))[:120]
        sched = ", ".join(s.get("Name", "") for s in (m.get("PositionSchedule") or []))
        summary = ((m.get("UserArea", {}).get("Details", {}) or {}).get("JobSummary")
                   or m.get("QualificationSummary") or "")
        out.append(_rec(
            "USAJOBS", m.get("PositionID"), m.get("PositionTitle"), m.get("OrganizationName"),
            loc, sched, False, m.get("PositionURI"), summary, m.get("PublicationStartDate"),
            {"name": "USAJOBS (US Gov)", "link": m.get("PositionURI") or "https://www.usajobs.gov"}))
    return out


# Feed adapters return a general (query-independent) feed → cache once, share
# across queries. Keyword adapters do real server-side search → cache per query.
FEED_ADAPTERS = [himalayas, remotive, remoteok, company_boards]
KEYWORD_ADAPTERS = [adzuna, jsearch, reed, usajobs]
ADAPTERS = FEED_ADAPTERS + KEYWORD_ADAPTERS


# --- fan-out + dedupe -------------------------------------------------------

def _dedupe_key(r):
    return (re.sub(r"\s+", " ", (r["title"] or "").lower()).strip(),
            (r["company"] or "").lower().strip())


def dedupe(records):
    seen, out = set(), []
    for r in records:
        k = _dedupe_key(r)
        if k in seen or not k[0]:
            continue
        seen.add(k)
        out.append(r)
    return out


def search_all(query, adapters=None):
    """Fan out across adapters concurrently, merge + dedupe. One failing source
    never sinks the search — it lands in sources_skipped."""
    adapters = adapters if adapters is not None else ADAPTERS
    results, ok, skipped = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(adapters))) as ex:
        futs = {ex.submit(a, query): getattr(a, "__name__", "source") for a in adapters}
        for fut in concurrent.futures.as_completed(futs, timeout=TIMEOUT + 10):
            name = futs[fut]
            try:
                results.extend(fut.result())
                ok.append(name)
            except Exception:
                skipped.append(name)
    return {"results": dedupe(results), "sources_ok": sorted(ok), "sources_skipped": sorted(skipped)}


# --- ranking (stdlib TF-IDF cosine; injection-immune bag-of-words) -----------

_WORD = re.compile(r"[a-z0-9+#.]+")
_STOP = set((
    "a an the and or of to in for with on at is are be as by from this that we you your our "
    "role job work works working experience year years team teams will who what when where "
    "have has had do does new use using etc per via not all any can may"
).split())

# Acronym / synonym expansion so distinctive but terse queries match real roles
# even without an LLM. Keys are lowercase tokens.
ACRONYMS = {
    "soc": ["security operations center", "security analyst", "cybersecurity", "siem",
            "incident response", "threat detection", "blue team"],
    "sec": ["security", "cybersecurity"],
    "infosec": ["information security", "cybersecurity"],
    "sre": ["site reliability engineer", "devops", "infrastructure"],
    "devops": ["site reliability", "infrastructure", "ci cd", "kubernetes"],
    "ml": ["machine learning"], "ai": ["artificial intelligence", "machine learning"],
    "nlp": ["natural language processing"], "qa": ["quality assurance", "test automation"],
    "pm": ["product manager", "project manager"], "ba": ["business analyst"],
    "ux": ["user experience", "product design"], "fe": ["frontend"], "be": ["backend"],
    "fullstack": ["full stack"], "swe": ["software engineer"],
}


# Generic job-title words: shared across unrelated roles, so matching one ALONE
# shouldn't qualify a result. The gate requires a non-role *domain* term too.
_ROLE_WORDS = set((
    "analyst engineer developer manager specialist lead coordinator associate director "
    "consultant administrator architect designer scientist officer representative agent "
    "assistant intern technician operator supervisor executive head chief vp president "
    "staff senior junior principal"
).split())


def _toks(s):
    return [w for w in _WORD.findall((s or "").lower()) if w not in _STOP and len(w) > 1]


def expand_query(query, llm=None):
    """Expand a terse query into a richer keyword set (acronyms + synonyms). Uses
    the configured LLM when available (the user's query is trusted input — no
    fence needed); always folds in a built-in acronym map. Returns ordered terms,
    base terms first."""
    base = _toks(query)
    extra = set()
    for w in base:
        for syn in ACRONYMS.get(w, []):
            extra.update(_toks(syn))
    if llm is not None:
        try:
            prompt = (
                "Expand this job-search query into 8-14 lowercase search keywords and synonyms, "
                "including expanded acronyms and closely related role titles and core skills. "
                "Return ONLY a comma-separated list.\n\nQuery: " + query)
            txt = llm.invoke(prompt).content
            if isinstance(txt, list):
                txt = " ".join(str(x) for x in txt)
            for piece in re.split(r"[,\n;]+", str(txt)):
                extra.update(_toks(piece))
        except Exception:
            pass
    return list(dict.fromkeys(base + sorted(extra - set(base)))) or base


def rank_matches(query, results, min_score=0.0, gate=False):
    """Score results by TF-IDF cosine relevance, sorted desc. `query` may be a
    string or a pre-expanded term list.

    With gate=True, drop results that don't contain a *distinctive* query term
    (one that's rare in the candidate pool) — this is what stops generic words
    like 'analyst' from floating unrelated jobs to the top. With min_score>0,
    drop weak matches. If gating/thresholding removes everything, fall back to
    the top results by raw score so the UI is never blank when matches exist."""
    results = [dict(r) for r in results]
    terms = list(query) if isinstance(query, (list, tuple)) else _toks(query)
    if not terms or not results:
        for r in results:
            r["score"] = 0.0
        return results
    doc_toks = [_toks((r.get("title", "") + " ") * 3 + r.get("description", "")) for r in results]
    doc_sets = [set(t) for t in doc_toks]
    n = len(results)
    df = Counter()
    for s in doc_sets:
        df.update(s)
    default_idf = math.log(n + 1) + 1
    idf = {w: math.log((n + 1) / (df[w] + 1)) + 1 for w in df}
    # distinctive query terms = present, reasonably rare, and NOT a generic role
    # word. A result must contain one of these to pass the gate, so a shared
    # title suffix like "analyst" can't float an unrelated job to the top.
    distinctive = {t for t in terms
                   if t not in _ROLE_WORDS and 0 < df.get(t, 0) <= max(2, n * 0.15)}

    def vec(toks):
        tf = Counter(toks)
        ln = max(1, len(toks))
        return {w: (c / ln) * idf.get(w, default_idf) for w, c in tf.items()}

    qv = vec(terms)
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    for r, toks, dset in zip(results, doc_toks, doc_sets):
        dv = vec(toks)
        dn = math.sqrt(sum(v * v for v in dv.values())) or 1.0
        r["score"] = round(sum(qv.get(w, 0.0) * dv.get(w, 0.0) for w in qv) / (qn * dn), 4)
        r["_keep"] = (not gate) or (not distinctive) or bool(distinctive & dset)

    kept = [r for r in results if r["_keep"] and r["score"] >= min_score]
    if not kept and (gate or min_score):  # nothing passed — show the closest anyway
        kept = sorted((r for r in results if r["score"] > 0), key=lambda r: r["score"], reverse=True)[:20]
    kept.sort(key=lambda r: r["score"], reverse=True)
    for r in kept:
        r.pop("_keep", None)
    return kept
