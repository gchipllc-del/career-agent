import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ats


JOB = """Senior Python Engineer
We require strong Python and Django experience. Must have PostgreSQL and Docker.
Kubernetes is a plus. 5 years of backend experience."""


class TestScore(unittest.TestCase):
    def test_empty_jd_scores_full(self):
        s = ats.score("", "anything")
        self.assertEqual(s["score"], 100)
        self.assertTrue(s["passes"])

    def test_strong_match_scores_high(self):
        resume = ("Senior Python Engineer. Built Django services on PostgreSQL and Docker, "
                  "deployed to Kubernetes. 6 years backend experience.")
        s = ats.score(JOB, resume)
        self.assertGreaterEqual(s["score"], ats.ATS_TARGET)
        self.assertTrue(s["passes"])
        self.assertEqual(s["missing_required"], [])

    def test_weak_match_flags_required_gaps(self):
        resume = "Marketing manager. Ran campaigns and managed budgets."
        s = ats.score(JOB, resume)
        self.assertLess(s["score"], ats.ATS_TARGET)
        self.assertFalse(s["passes"])
        # required tech keywords should surface as missing
        self.assertTrue(any(k in s["missing_required"] for k in ("python", "django", "postgresql")))

    def test_title_match_signal(self):
        s_match = ats.score(JOB, "Senior Python Engineer with Django, PostgreSQL, Docker, Kubernetes backend.")
        self.assertTrue(s_match["title_match"])
        s_nomatch = ats.score(JOB, "Cook at a restaurant.")
        self.assertFalse(s_nomatch["title_match"])


class TestFeedback(unittest.TestCase):
    def test_feedback_names_gaps_and_forbids_fabrication(self):
        s = ats.score(JOB, "Marketing manager.")
        fb = ats.feedback(s)
        self.assertIn("ATS score", fb)
        self.assertIn("do NOT invent", fb)

    def test_feedback_lists_required_first(self):
        s = ats.score(JOB, "Python developer.")  # has python, missing django/postgres/docker
        fb = ats.feedback(s)
        self.assertIn("REQUIRED", fb)


if __name__ == "__main__":
    unittest.main()
