"""Tests for jobs_search.py — source normalizers, fan-out/dedupe, TF-IDF ranking.

Network-free: each adapter's HTTP call (jobs_search._get) is monkeypatched with
a canned payload shaped like the real API response. Zero installs.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs_search as js  # noqa: E402


class _Patch(unittest.TestCase):
    def patch_get(self, payload):
        orig = js._get
        js._get = lambda url, *a, **k: payload
        self.addCleanup(lambda: setattr(js, "_get", orig))


class TestNormalizers(_Patch):
    def test_himalayas(self):
        self.patch_get({"jobs": [{
            "guid": "h1", "title": "Senior Python Engineer", "companyName": "Acme",
            "employmentType": "Full Time", "applicationLink": "https://acme.com/apply",
            "locationRestrictions": ["US", "EU"], "description": "Build APIs", "pubDate": "2026-06-01"}]})
        r = js.himalayas("python")[0]
        self.assertEqual(r["source"], "Himalayas")
        self.assertEqual(r["title"], "Senior Python Engineer")
        self.assertEqual(r["company"], "Acme")
        self.assertEqual(r["url"], "https://acme.com/apply")
        self.assertEqual(r["location"], "US, EU")
        self.assertEqual(r["employment_type"], "Full Time")
        self.assertEqual(r["attribution"]["name"], "Himalayas")

    def test_remotive(self):
        self.patch_get({"jobs": [{
            "id": 42, "title": "Backend Dev", "company_name": "Globex", "job_type": "contract",
            "url": "https://remotive.com/x", "candidate_required_location": "Worldwide",
            "description": "<p>Go &amp; Python</p>", "publication_date": "2026-05-01"}]})
        r = js.remotive("backend")[0]
        self.assertEqual(r["company"], "Globex")
        self.assertEqual(r["employment_type"], "contract")
        self.assertEqual(r["description"], "Go & Python")  # html stripped + entity unescaped

    def test_remoteok_skips_legal_object(self):
        self.patch_get([
            {"legal": "Please link back to Remote OK"},  # index 0 must be skipped
            {"id": "ro1", "position": "Rust Engineer", "company": "Initech",
             "apply_url": "https://ro/apply", "location": "Remote", "tags": ["rust", "contract"],
             "description": "Systems work", "date": "2026-06-10"}])
        rows = js.remoteok("rust")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Rust Engineer")
        self.assertEqual(rows[0]["employment_type"], "contract")  # inferred from tags
        self.assertTrue(rows[0]["attribution"].get("direct"))

    def test_greenhouse_unescapes_content(self):
        self.patch_get({"jobs": [{
            "id": 7, "title": "Data Engineer", "company_name": "Stripe",
            "absolute_url": "https://boards.greenhouse.io/stripe/jobs/7",
            "location": {"name": "Remote US"}, "content": "&lt;p&gt;Spark &amp; SQL&lt;/p&gt;",
            "updated_at": "2026-06-09"}]})
        r = js._greenhouse("stripe")[0]
        self.assertEqual(r["title"], "Data Engineer")
        self.assertEqual(r["url"], "https://boards.greenhouse.io/stripe/jobs/7")
        self.assertEqual(r["description"], "Spark & SQL")
        self.assertTrue(r["remote"])

    def test_lever(self):
        self.patch_get([{
            "id": "lv1", "text": "Platform Engineer", "hostedUrl": "https://jobs.lever.co/x/lv1",
            "categories": {"commitment": "Full-time", "location": "Remote"},
            "descriptionPlain": "Kubernetes and Go", "createdAt": 1700000000000,
            "workplaceType": "remote"}])
        r = js._lever("acme")[0]
        self.assertEqual(r["title"], "Platform Engineer")
        self.assertEqual(r["company"], "Acme")
        self.assertEqual(r["employment_type"], "Full-time")
        self.assertTrue(r["remote"])

    def test_jsearch_normalizes_google_jobs(self):
        os.environ["RAPIDAPI_KEY"] = "testkey"
        self.addCleanup(os.environ.pop, "RAPIDAPI_KEY", None)
        self.patch_get({"data": [{
            "job_id": "js1", "job_title": "SOC Analyst", "employer_name": "Acme Security",
            "job_city": "Austin", "job_state": "TX", "job_country": "US",
            "job_employment_type": "FULLTIME", "job_is_remote": True,
            "job_apply_link": "https://www.linkedin.com/jobs/view/123",
            "job_description": "Monitor SIEM, incident response", "job_publisher": "LinkedIn",
            "job_posted_at_datetime_utc": "2026-06-01T00:00:00Z"}]})
        r = js.jsearch("soc analyst")[0]
        self.assertEqual(r["source"], "Google Jobs")
        self.assertEqual(r["title"], "SOC Analyst")
        self.assertEqual(r["company"], "Acme Security")
        self.assertEqual(r["location"], "Austin, TX, US")
        self.assertEqual(r["employment_type"], "FULLTIME")
        self.assertTrue(r["remote"])
        self.assertIn("LinkedIn", r["attribution"]["name"])  # original publisher

    def test_jsearch_self_skips_without_key(self):
        os.environ.pop("RAPIDAPI_KEY", None)
        self.assertEqual(js.jsearch("anything"), [])

    def test_reed_normalizes(self):
        os.environ["REED_API_KEY"] = "k"
        self.addCleanup(os.environ.pop, "REED_API_KEY", None)
        self.patch_get({"results": [{
            "jobId": 111, "jobTitle": "SOC Analyst", "employerName": "Acme",
            "locationName": "London", "contractType": "Permanent",
            "jobUrl": "https://www.reed.co.uk/jobs/111", "jobDescription": "SIEM monitoring",
            "date": "01/06/2026"}]})
        r = js.reed("soc")[0]
        self.assertEqual(r["source"], "Reed")
        self.assertEqual(r["title"], "SOC Analyst")
        self.assertEqual(r["company"], "Acme")
        self.assertEqual(r["url"], "https://www.reed.co.uk/jobs/111")
        self.assertEqual(r["employment_type"], "Permanent")

    def test_reed_self_skips_without_key(self):
        os.environ.pop("REED_API_KEY", None)
        self.assertEqual(js.reed("x"), [])

    def test_usajobs_normalizes(self):
        os.environ["USAJOBS_API_KEY"] = "k"
        os.environ["USAJOBS_EMAIL"] = "a@b.com"
        self.addCleanup(os.environ.pop, "USAJOBS_API_KEY", None)
        self.addCleanup(os.environ.pop, "USAJOBS_EMAIL", None)
        self.patch_get({"SearchResult": {"SearchResultItems": [{"MatchedObjectDescriptor": {
            "PositionID": "GS-7", "PositionTitle": "Cybersecurity Analyst", "OrganizationName": "DHS",
            "PositionURI": "https://www.usajobs.gov/job/1",
            "PositionLocation": [{"LocationName": "Washington, DC"}],
            "PositionSchedule": [{"Name": "Full-Time"}], "QualificationSummary": "Defend networks",
            "PublicationStartDate": "2026-06-01"}}]}})
        r = js.usajobs("cyber")[0]
        self.assertEqual(r["source"], "USAJOBS")
        self.assertEqual(r["title"], "Cybersecurity Analyst")
        self.assertEqual(r["company"], "DHS")
        self.assertEqual(r["location"], "Washington, DC")
        self.assertEqual(r["employment_type"], "Full-Time")
        self.assertIn("US Gov", r["attribution"]["name"])

    def test_usajobs_self_skips_without_keys(self):
        os.environ.pop("USAJOBS_API_KEY", None)
        os.environ.pop("USAJOBS_EMAIL", None)
        self.assertEqual(js.usajobs("x"), [])


def _rec(title, company="Acme", desc=""):
    return {"title": title, "company": company, "description": desc, "url": "u"}


class TestFanOutDedupe(unittest.TestCase):
    def test_merge_dedupe_and_graceful_failure(self):
        dup = _rec("Backend Engineer", "Globex")
        def a(q): return [_rec("Frontend Engineer"), dict(dup)]
        def b(q): return [dict(dup), _rec("Data Scientist")]
        def boom(q): raise RuntimeError("source down")
        res = js.search_all("x", adapters=[a, b, boom])
        self.assertEqual(set(res["sources_ok"]), {"a", "b"})
        self.assertEqual(res["sources_skipped"], ["boom"])
        titles = sorted(r["title"] for r in res["results"])
        self.assertEqual(titles, ["Backend Engineer", "Data Scientist", "Frontend Engineer"])  # dup collapsed

    def test_drops_titleless_records(self):
        def a(q): return [_rec(""), _rec("Real Job")]
        res = js.search_all("x", adapters=[a])
        self.assertEqual([r["title"] for r in res["results"]], ["Real Job"])


class TestRanking(unittest.TestCase):
    def test_relevant_ranks_first(self):
        rows = [
            _rec("Marketing Manager", "M", "brand campaigns and social media"),
            _rec("Senior Python Backend Engineer", "B", "python django postgres apis backend"),
            _rec("Sales Rep", "S", "quota pipeline outbound calls"),
        ]
        ranked = js.rank_matches("python backend engineer", rows)
        self.assertEqual(ranked[0]["title"], "Senior Python Backend Engineer")
        self.assertGreater(ranked[0]["score"], ranked[-1]["score"])
        self.assertGreaterEqual(ranked[0]["score"], 0.0)

    def test_empty_query_scores_zero(self):
        ranked = js.rank_matches("", [_rec("Anything")])
        self.assertEqual(ranked[0]["score"], 0.0)

    def test_ranking_does_not_mutate_input(self):
        rows = [_rec("X", desc="python")]
        js.rank_matches("python", rows)
        self.assertNotIn("score", rows[0])  # operated on copies


class TestExpandAndGate(unittest.TestCase):
    def test_expand_query_expands_acronym(self):
        terms = js.expand_query("soc analyst")  # no LLM -> built-in acronym map
        self.assertIn("soc", terms)
        self.assertIn("security", terms)
        self.assertIn("cybersecurity", terms)

    def test_gate_filters_generic_analysts(self):
        terms = js.expand_query("soc analyst")
        rows = [
            _rec("SOC Analyst", "A", "security operations center siem incident response threat"),
            _rec("Billing Analyst", "B", "invoices accounts receivable reconciliation"),
            _rec("Data Analyst", "C", "dashboards sql reporting tableau"),
        ]
        kept = js.rank_matches(terms, rows, min_score=0.0, gate=True)
        titles = [r["title"] for r in kept]
        self.assertIn("SOC Analyst", titles)
        self.assertNotIn("Billing Analyst", titles)   # generic 'analyst' alone is gated out
        self.assertNotIn("Data Analyst", titles)

    def test_gate_disables_itself_when_no_distinctive_term_in_pool(self):
        # When the query's distinctive terms aren't in the pool at all, the gate
        # disables itself rather than nuking everything (UI flags these 'weak').
        rows = [_rec("Data Analyst", "C", "reports dashboards"),
                _rec("Business Analyst", "D", "process reports")]
        kept = js.rank_matches(["soc", "security", "analyst"], rows, min_score=0.0, gate=True)
        self.assertEqual(len(kept), 2)


if __name__ == "__main__":
    unittest.main()
