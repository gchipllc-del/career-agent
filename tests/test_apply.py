import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import apply


class TestDetectAts(unittest.TestCase):
    def test_known_boards(self):
        self.assertEqual(apply.detect_ats("https://boards.greenhouse.io/acme/jobs/123"), "greenhouse")
        self.assertEqual(apply.detect_ats("https://jobs.lever.co/acme/abc"), "lever")
        self.assertEqual(apply.detect_ats("https://jobs.ashbyhq.com/acme/x"), "ashby")
        self.assertEqual(apply.detect_ats("https://acme.wd1.myworkdayjobs.com/x"), "workday")

    def test_generic_and_none(self):
        self.assertEqual(apply.detect_ats("https://careers.example.com/apply"), "generic")
        self.assertIsNone(apply.detect_ats(""))
        self.assertIsNone(apply.detect_ats("not a url"))


class TestCompliance(unittest.TestCase):
    def test_ban_risk_boards_blocked(self):
        for u in ("https://www.linkedin.com/jobs/view/1", "https://www.indeed.com/viewjob?jk=1",
                  "https://www.glassdoor.com/job/1", "https://www.dice.com/jobs/1"):
            ok, reason = apply.is_automatable(u)
            self.assertFalse(ok, u)
            self.assertTrue(reason)

    def test_ats_allowed(self):
        ok, _ = apply.is_automatable("https://boards.greenhouse.io/acme/jobs/1")
        self.assertTrue(ok)

    def test_empty_blocked(self):
        ok, _ = apply.is_automatable("")
        self.assertFalse(ok)


class TestFillPlan(unittest.TestCase):
    PROFILE = {"full_name": "Jane Q. Candidate", "email": "jane@example.com",
               "phone": "555-0102", "linkedin": "https://linkedin.com/in/jane",
               "resume_path": "/tmp/jane.docx"}

    def test_skips_empty_fields(self):
        plan = apply.build_fill_plan({"email": "a@b.com"}, "generic")
        keys = {f["key"] for f in plan}
        self.assertIn("email", keys)
        self.assertNotIn("phone", keys)  # no phone provided -> skipped

    def test_includes_resume_upload(self):
        plan = apply.build_fill_plan(self.PROFILE, "greenhouse")
        resume = [f for f in plan if f["key"] == "resume"]
        self.assertEqual(len(resume), 1)
        self.assertEqual(resume[0]["type"], "upload")
        self.assertEqual(resume[0]["value"], "/tmp/jane.docx")

    def test_ats_specific_selectors_first(self):
        plan = apply.build_fill_plan(self.PROFILE, "greenhouse")
        email = next(f for f in plan if f["key"] == "email")
        self.assertEqual(email["selectors"][0], "#email")  # greenhouse-specific leads

    def test_name_parts_split(self):
        plan = apply.build_fill_plan({"full_name": "Ada Lovelace", "email": "a@b.com"}, "generic")
        first = next(f for f in plan if f["key"] == "first_name")
        last = next(f for f in plan if f["key"] == "last_name")
        self.assertEqual(first["value"], "Ada")
        self.assertEqual(last["value"], "Lovelace")


class TestNote(unittest.TestCase):
    def test_automatable_note_has_link_and_submit_language(self):
        note = apply.build_note("SOC Analyst", "Acme", "https://x.co/apply", True)
        self.assertIn("https://x.co/apply", note)
        self.assertIn("Submit", note)

    def test_packet_note_includes_reason(self):
        note = apply.build_note("SOC Analyst", "Acme", "https://linkedin.com/x", False, "LinkedIn bans automation")
        self.assertIn("LinkedIn bans automation", note)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = apply.ApplyStore(":memory:")

    def test_profile_roundtrip(self):
        self.assertEqual(self.store.get_profile(), {})
        self.store.set_profile({"email": "a@b.com"})
        self.assertEqual(self.store.get_profile()["email"], "a@b.com")

    def test_application_lifecycle(self):
        rec = self.store.add_application({"job_title": "SOC Analyst", "company": "Acme",
                                          "url": "https://x.co", "status": "prepared"})
        self.assertEqual(rec["status"], "prepared")
        self.assertTrue(rec["id"])
        self.assertEqual(len(self.store.list_applications()), 1)
        upd = self.store.set_status(rec["id"], "submitted")
        self.assertEqual(upd["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
