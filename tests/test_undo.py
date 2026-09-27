"""Undo safety: what gets reversed, what is left alone, and how values are compared.

    python3 tests/test_undo.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cwa_organizer.calibre_applier import _same  # noqa: E402
from cwa_organizer.runner import build_undo_ops  # noqa: E402


def rec(book_id=1, status="applied", before=None, after=None, title="A Book"):
    return {"book_id": book_id, "title": title, "status": status,
            "before": before or {}, "after": after or {}}


class BuildUndoOps(unittest.TestCase):
    def test_op_expects_what_the_run_wrote_and_is_strict(self):
        ops, notes = build_undo_ops([rec(before={"title": "Old", "tags": []},
                                         after={"title": "New", "tags": ["Light Novel"]})])
        self.assertEqual(len(ops), 1)
        op = ops[0]
        self.assertEqual(op["fields"], {"title": "Old", "tags": []})
        self.assertEqual(op["expect"], {"title": "New", "tags": ["Light Novel"]})
        self.assertTrue(op["strict"])
        self.assertEqual(notes, [])

    def test_newest_first(self):
        ops, _ = build_undo_ops([rec(book_id=1, after={"tags": ["a"]}),
                                 rec(book_id=2, after={"tags": ["b"]})])
        self.assertEqual([o["book_id"] for o in ops], [2, 1])

    def test_failed_and_skipped_records_are_not_reversed(self):
        ops, _ = build_undo_ops([rec(status="skipped", after={}), rec(status="failed", after={})])
        self.assertEqual(ops, [])

    def test_partial_records_reverse_only_what_was_written(self):
        ops, _ = build_undo_ops([rec(status="partial", before={"tags": [], "title": "Old"},
                                     after={"tags": ["x"]})])
        self.assertEqual(ops[0]["fields"], {"tags": []})

    def test_cover_reversal_expects_the_image_fingerprint(self):
        digest = "a" * 40
        ops, notes = build_undo_ops([rec(before={"cover": None}, after={"cover": digest})])
        self.assertEqual(ops[0]["fields"], {"cover": None})
        self.assertEqual(ops[0]["expect"], {"cover": digest})
        self.assertEqual(notes, [])

    def test_cover_left_alone_when_the_journal_predates_fingerprints(self):
        ops, notes = build_undo_ops([rec(before={"cover": None}, after={"cover": "/tmp/some/cover.jpg"})])
        self.assertEqual(ops, [])
        self.assertIn("cover left as is", notes[0])

    def test_a_cover_that_existed_before_is_never_deleted(self):
        ops, notes = build_undo_ops([rec(before={"cover": True}, after={"cover": "b" * 40})])
        self.assertEqual(ops, [])
        self.assertIn("can't be restored", notes[0])


class StrictComparison(unittest.TestCase):
    """Undo compares exactly; a run's own writes may compare case-insensitively."""

    def test_case_only_edit_counts_as_a_change_under_strict(self):
        self.assertFalse(_same("title", "Kizumonogatari", "KIZUMONOGATARI", strict=True))
        self.assertFalse(_same("authors", ["Nisioisin"], ["NISIOISIN"], strict=True))
        self.assertFalse(_same("tags", ["Light Novel"], ["light novel"], strict=True))
        self.assertFalse(_same("series", "Monogatari", "monogatari", strict=True))

    def test_same_values_still_match_under_strict(self):
        self.assertTrue(_same("title", "Same", "Same", strict=True))
        self.assertTrue(_same("authors", ["A", "B"], ["A", "B"], strict=True))
        self.assertTrue(_same("tags", ["B", "A"], ["A", "B"], strict=True))
        self.assertTrue(_same("series_index", 3.0, 3.0, strict=True))

    def test_a_real_edit_is_a_change_either_way(self):
        for strict in (False, True):
            self.assertFalse(_same("title", "Vol. 1", "Vol. 2", strict=strict))
            self.assertFalse(_same("tags", ["A"], ["A", "B"], strict=strict))
            self.assertFalse(_same("comments", "", "text", strict=strict))

    def test_calibre_case_folding_still_applies_to_ordinary_runs(self):
        self.assertTrue(_same("authors", ["Nisioisin"], ["NISIOISIN"]))
        self.assertTrue(_same("series", "Monogatari", "monogatari"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
