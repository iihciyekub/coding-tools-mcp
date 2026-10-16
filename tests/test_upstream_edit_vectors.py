"""Pure regression vectors adapted from xyTom/coding-tools-mcp d7c2dda (Apache-2.0)."""

from __future__ import annotations
import unittest
from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.changes import (
    apply_line_edits,
    content_lines,
    parse_changes,
    split_lines,
)
from coding_tools_mcp.patching import (
    PatchHunk,
    apply_update_hunks_detailed,
    parse_patch,
)


def edit(**fields: object) -> dict[str, object]:
    return dict(fields)


class LineSemanticsTests(unittest.TestCase):
    """The line model has to be the one read_file reports, exactly."""

    def test_empty_content_is_zero_lines_not_one_blank_line(self) -> None:
        # "".split("\n") is [""], which would leave a stray blank line behind
        # every emptied range.
        self.assertEqual(content_lines(""), [])
        self.assertEqual(content_lines("a"), ["a"])

    def test_a_trailing_newline_adds_a_blank_line(self) -> None:
        self.assertEqual(content_lines("a\n"), ["a", ""])

    def test_replacement_content_normalizes_crlf_and_cr(self) -> None:
        self.assertEqual(content_lines("a\r\nb\rc"), ["a", "b", "c"])

    def test_line_counting_matches_read_file(self) -> None:
        self.assertEqual(split_lines("a\nb\n"), (["a", "b"], True))
        self.assertEqual(split_lines("a\nb"), (["a", "b"], False))
        self.assertEqual(split_lines(""), ([], False))


class LineEditTests(unittest.TestCase):
    def apply(self, text: str, raw_edits: list[dict[str, object]]) -> str:
        change = parse_changes(
            [{"action": "edit", "path": "f.txt", "revision": "r", "edits": raw_edits}]
        )[0]
        return apply_line_edits(text, change.edits, "f.txt").content

    def test_replace_a_range(self) -> None:
        result = self.apply(
            "a\nb\nc\n", [edit(op="replace", start_line=2, end_line=2, content="B")]
        )
        self.assertEqual(result, "a\nB\nc\n")

    def test_replace_with_empty_content_deletes_the_range(self) -> None:
        result = self.apply(
            "a\nb\nc\n", [edit(op="replace", start_line=2, end_line=2, content="")]
        )
        self.assertEqual(result, "a\nc\n")

    def test_end_line_defaults_to_start_line(self) -> None:
        self.assertEqual(
            self.apply("a\nb\n", [edit(op="replace", start_line=1, content="A")]),
            "A\nb\n",
        )

    def test_insert_after_zero_writes_at_the_beginning(self) -> None:
        self.assertEqual(
            self.apply("a\n", [edit(op="insert_after", line=0, content="z")]), "z\na\n"
        )

    def test_insert_before_past_the_last_line_appends(self) -> None:
        self.assertEqual(
            self.apply("a\nb\n", [edit(op="insert_before", line=3, content="c")]),
            "a\nb\nc\n",
        )

    def test_insert_after_the_last_line_appends(self) -> None:
        self.assertEqual(
            self.apply("a\nb\n", [edit(op="insert_after", line=2, content="c")]),
            "a\nb\nc\n",
        )

    def test_delete_removes_the_range(self) -> None:
        self.assertEqual(
            self.apply("a\nb\nc\n", [edit(op="delete", start_line=1, end_line=2)]),
            "c\n",
        )

    def test_every_edit_addresses_the_file_as_read(self) -> None:
        # Both edits name original line numbers; the caller does not have to
        # predict how the first one shifts the second.
        result = self.apply(
            "a\nb\nc\nd\n",
            [
                edit(op="replace", start_line=1, end_line=1, content="one\ntwo\nthree"),
                edit(op="replace", start_line=4, end_line=4, content="D"),
            ],
        )
        self.assertEqual(result, "one\ntwo\nthree\nb\nc\nD\n")

    def test_a_file_with_no_trailing_newline_keeps_none(self) -> None:
        self.assertEqual(
            self.apply("a\nb", [edit(op="replace", start_line=1, content="A")]), "A\nb"
        )

    def test_crlf_and_bom_survive(self) -> None:
        result = self.apply(
            "\ufeffa\r\nb\r\n", [edit(op="replace", start_line=2, content="B")]
        )
        self.assertEqual(result, "\ufeffa\r\nB\r\n")

    def test_deleting_every_line_leaves_an_empty_file(self) -> None:
        self.assertEqual(
            self.apply("a\nb\n", [edit(op="delete", start_line=1, end_line=2)]), ""
        )

    def test_overlapping_edits_are_refused(self) -> None:
        with self.assertRaises(ToolFailure) as raised:
            self.apply(
                "a\nb\nc\n",
                [
                    edit(op="replace", start_line=1, end_line=2, content="x"),
                    edit(op="replace", start_line=2, end_line=3, content="y"),
                ],
            )
        self.assertEqual(raised.exception.code, "PATCH_HUNKS_OVERLAP")

    def test_two_insertions_at_one_point_are_refused(self) -> None:
        with self.assertRaises(ToolFailure) as raised:
            self.apply(
                "a\nb\n",
                [
                    edit(op="insert_after", line=1, content="x"),
                    edit(op="insert_before", line=2, content="y"),
                ],
            )
        self.assertEqual(raised.exception.code, "PATCH_HUNKS_OVERLAP")

    def test_an_edit_past_the_end_names_the_real_line_count(self) -> None:
        with self.assertRaises(ToolFailure) as raised:
            self.apply("a\nb\n", [edit(op="replace", start_line=9, content="x")])
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(raised.exception.details["total_lines"], 2)

    def test_changed_ranges_name_the_new_line_numbers(self) -> None:
        change = parse_changes(
            [
                {
                    "action": "edit",
                    "path": "f.txt",
                    "revision": "r",
                    "edits": [edit(op="insert_after", line=1, content="x\ny")],
                }
            ]
        )[0]
        outcome = apply_line_edits("a\nb\n", change.edits, "f.txt")
        self.assertEqual(
            outcome.changed_ranges,
            [{"start_line": 2, "end_line": 3, "added_lines": 2, "removed_lines": 0}],
        )

    def test_an_insertion_in_front_of_a_replacement_is_numbered_in_order(self) -> None:
        # The insertion is an empty span at the replacement's first line, so
        # the two share a start. Whichever order the caller listed them in,
        # the inserted line lands first and both ranges have to say so.
        for order in ([0, 1], [1, 0]):
            with self.subTest(order=order):
                listed = [
                    edit(op="replace", start_line=1, end_line=3, content="P"),
                    edit(op="insert_after", line=0, content="X"),
                ]
                change = parse_changes(
                    [
                        {
                            "action": "edit",
                            "path": "f.txt",
                            "revision": "r",
                            "edits": [listed[position] for position in order],
                        }
                    ]
                )[0]
                outcome = apply_line_edits("a\nb\nc\nd\n", change.edits, "f.txt")
                self.assertEqual(outcome.content, "X\nP\nd\n")
                self.assertEqual(
                    outcome.changed_ranges,
                    [
                        {
                            "start_line": 1,
                            "end_line": 1,
                            "added_lines": 1,
                            "removed_lines": 0,
                        },
                        {
                            "start_line": 2,
                            "end_line": 2,
                            "added_lines": 1,
                            "removed_lines": 3,
                        },
                    ],
                )


def envelope(body: str) -> str:
    return "*** Begin Patch\n" + body + "*** End Patch"


class ParserAndLocatorUnitTests(unittest.TestCase):
    def test_punctuation_and_blank_anchors_are_not_already_applied_evidence(
        self,
    ) -> None:
        """Do not count any matched punctuation or blank scope as a second evidence line."""
        for anchor in ("}", ")", "[];,", "", " \t"):
            with self.subTest(anchor=anchor):
                with self.assertRaises(ToolFailure) as raised:
                    apply_update_hunks_detailed(
                        anchor + "\nx = 1\n",
                        [PatchHunk(["-missing = 0", "+x = 1"], anchor)],
                    )
                self.assertEqual(raised.exception.code, "PATCH_CONTEXT_NOT_FOUND")

    def test_substantive_anchor_still_counts_as_already_applied_evidence(self) -> None:
        """Accept a single result line when its adjacent anchor identifies a real section."""
        original = "[server]\ntimeout = 30\n"
        outcome = apply_update_hunks_detailed(
            original, [PatchHunk(["-timeout = 20", "+timeout = 30"], "[server]")]
        )
        self.assertEqual(outcome.content, original)
        self.assertEqual(outcome.already_applied_hunks, [0])
        self.assertEqual(outcome.applied_hunks, 0)
        self.assertEqual(outcome.changed_ranges, [])

    def test_consecutive_headers_are_kept_as_nested_scopes(self) -> None:
        """Verify consecutive anchor headers form nested scopes that reset for the next hunk."""
        operations = parse_patch(
            envelope(
                "*** Update File: a.py\n@@ class B:\n@@ def run(self):\n-x\n+y\n@@ other\n-p\n+q\n"
            )
        )
        first, second = operations[0].hunks
        self.assertEqual(first.outer_scopes, ("class B:",))
        self.assertEqual(first.scope, "def run(self):")
        self.assertEqual(second.outer_scopes, ())
        self.assertEqual(second.scope, "other")

    def test_first_match_after_an_anchor_wins(self) -> None:
        """Verify an anchored hunk selects the first matching body after its anchor."""
        outcome = apply_update_hunks_detailed(
            "x = 1\nanchor\nx = 1\nx = 1\n", [PatchHunk(["-x = 1", "+x = 2"], "anchor")]
        )
        self.assertEqual(outcome.content, "x = 1\nanchor\nx = 2\nx = 1\n")

    def test_unanchored_ambiguity_after_the_cursor_is_reported(self) -> None:
        """Verify an unanchored duplicate body raises PATCH_CONTEXT_AMBIGUOUS."""
        with self.assertRaises(ToolFailure) as raised:
            apply_update_hunks_detailed("y\nx = 1\nx = 1\n", [["-x = 1", "+x = 2"]])
        self.assertEqual(raised.exception.code, "PATCH_CONTEXT_AMBIGUOUS")
