"""Red-team harness for the injection defense (C7).

Runs the labeled corpus in fixtures/injection_corpus.json against core.py's
advisory scan + structural fence. It enforces three properties:

  * direct injections are caught by the regex layer (regression guard),
  * benign job postings are NOT flagged (precision / false-positive guard),
  * the fence structurally neutralizes EVERY injection — even ones the advisory
    scan misses — because that's the actual security boundary.

The 'evasion' subset is intentionally hard for the regex (paraphrases); it is
measured, not gated, to document the limit of the heuristic layer.

Zero installs:  python3 -m unittest discover -s tests
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "injection_corpus.json")
with open(_FIXTURE, encoding="utf-8") as _f:
    CORPUS = json.load(_f)["samples"]


def _subset(name):
    return [s for s in CORPUS if s["subset"] == name]


class TestCorpusShape(unittest.TestCase):
    def test_nontrivial_and_well_formed(self):
        self.assertGreaterEqual(len(CORPUS), 30)
        for s in CORPUS:
            self.assertIn(s["label"], ("injection", "benign"))
            self.assertTrue(s["text"].strip())

    def test_has_each_subset(self):
        names = {s["subset"] for s in CORPUS}
        self.assertTrue({"direct", "evasion", "unicode", "fence-attack", "benign"} <= names)


class TestRegexLayer(unittest.TestCase):
    def test_direct_injections_all_caught(self):
        """Regression guard: a broken/removed pattern shows up here."""
        missed = [s["text"] for s in _subset("direct") if not core.scan_injection(s["text"])]
        self.assertEqual(missed, [], f"direct injections the regex no longer catches: {missed}")

    def test_benign_postings_not_flagged(self):
        """Precision guard: legitimate postings must not trip the advisory scan."""
        fp = [(s["text"], core.scan_injection(s["text"])) for s in _subset("benign")
              if core.scan_injection(s["text"])]
        self.assertEqual(fp, [], f"false positives on benign postings: {fp}")


class TestUnicodeNormalization(unittest.TestCase):
    def test_obfuscations_bypass_raw_but_are_caught_after_normalize(self):
        for s in _subset("unicode"):
            with self.subTest(text=s["text"]):
                raw_hit = any(rx.search(s["text"]) for _, rx in core.INJECTION_PATTERNS)
                self.assertFalse(raw_hit, "should bypass the RAW regex (proves normalization is load-bearing)")
                self.assertTrue(core.scan_injection(s["text"]), "must be caught after normalize_untrusted")


class TestFenceIsStructural(unittest.TestCase):
    def test_fence_neutralizes_every_injection(self):
        """The real boundary: regardless of advisory-scan recall, the payload is
        wrapped as data and cannot forge the closing marker (random sentinel)."""
        for s in CORPUS:
            if s["label"] != "injection":
                continue
            with self.subTest(text=s["text"][:40]):
                fenced, sentinel = core.build_safe_spec(s["text"])
                open_m = f"<<UNTRUSTED_JOB_DATA::{sentinel}>>\n"
                close_m = f"\n<<END_UNTRUSTED_JOB_DATA::{sentinel}>>"
                self.assertTrue(fenced.startswith(open_m))
                self.assertTrue(fenced.endswith(close_m))
                inner = fenced[len(open_m):-len(close_m)]
                # a forged closer with a guessed sentinel can sit in the body, but
                # the REAL random-sentinel closer can never appear inside it.
                self.assertNotIn(f"<<END_UNTRUSTED_JOB_DATA::{sentinel}>>", inner)


class TestRecallReport(unittest.TestCase):
    def test_overall_recall_floor_and_report(self):
        inj = [s for s in CORPUS if s["label"] == "injection"]
        caught = sum(1 for s in inj if core.scan_injection(s["text"]))
        recall = caught / len(inj)
        ev = _subset("evasion")
        ev_caught = sum(1 for s in ev if core.scan_injection(s["text"]))
        print(f"\n[red-team] advisory-scan recall: {caught}/{len(inj)} = {recall:.0%} "
              f"(evasion subset {ev_caught}/{len(ev)} — intentionally hard; the fence "
              f"neutralizes all {len(inj)} regardless)")
        # Floor only — evasion paraphrases are expected to evade the heuristic.
        self.assertGreaterEqual(recall, 0.55)


if __name__ == "__main__":
    unittest.main()
