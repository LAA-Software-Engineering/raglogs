"""Unit tests for the Loghub-2.0 normalization benchmark scorer (pure)."""

from src.eval.loghub import (
    grouping_accuracy,
    load_structured_csv,
    score_system,
    template_count_ratio,
)


class TestGroupingAccuracy:
    def test_perfect_grouping(self):
        gt = ["E1", "E1", "E2"]
        pred = ["a", "a", "b"]  # same partition, different label names
        assert grouping_accuracy(gt, pred) == 1.0

    def test_over_merge(self):
        # Ground truth has two groups; prediction merges them into one.
        gt = ["E1", "E1", "E2"]
        pred = ["x", "x", "x"]
        # No message's predicted group ({0,1,2}) equals its gt group, so GA = 0.
        assert grouping_accuracy(gt, pred) == 0.0

    def test_fragmentation(self):
        # One ground-truth group fragmented into two predicted groups.
        gt = ["E1", "E1", "E1", "E1"]
        pred = ["a", "a", "b", "b"]
        assert grouping_accuracy(gt, pred) == 0.0

    def test_partial(self):
        # E1's two messages are split apart (both wrong); the E2 singleton is
        # grouped correctly -> 1 of 3.
        gt = ["E1", "E1", "E2"]
        pred = ["a", "b", "c"]
        assert grouping_accuracy(gt, pred) == 1 / 3

    def test_empty(self):
        assert grouping_accuracy([], []) == 0.0


class TestTemplateRatio:
    def test_fragmentation_ratio_gt_one(self):
        assert template_count_ratio(["E1", "E1"], ["a", "b"]) == 2.0

    def test_over_merge_ratio_lt_one(self):
        assert template_count_ratio(["E1", "E2"], ["a", "a"]) == 0.5

    def test_exact(self):
        assert template_count_ratio(["E1", "E2"], ["a", "b"]) == 1.0


class TestLoadStructuredCsv:
    def test_reads_content_and_eventid(self):
        text = (
            "LineId,Content,EventId,EventTemplate\n"
            "1,Received block blk_123 of size 456,E5,Received block <*> of size <*>\n"
            "2,Received block blk_789 of size 111,E5,Received block <*> of size <*>\n"
        )
        rows = load_structured_csv(text)
        assert len(rows) == 2
        assert rows[0] == ("Received block blk_123 of size 456", "E5")

    def test_falls_back_to_template_without_eventid(self):
        text = "Content,EventTemplate\nfoo 1,foo <*>\n"
        rows = load_structured_csv(text)
        assert rows[0][1] == "foo <*>"

    def test_skips_blank_content(self):
        text = "Content,EventId\n,E1\nreal,E2\n"
        rows = load_structured_csv(text)
        assert rows == [("real", "E2")]


class TestScoreSystem:
    def test_end_to_end_on_synthetic_rows(self):
        # Two ground-truth templates; raglogs should normalize the numeric IDs
        # so each template collapses to one fingerprint.
        rows = [
            ("user 111 logged in", "E1"),
            ("user 222 logged in", "E1"),
            ("disk sda full", "E2"),
        ]
        res = score_system("Synthetic", rows)
        assert res.n_lines == 3
        assert res.ground_truth_templates == 2
        # "user <id> logged in" is one fingerprint; "disk sda full" another.
        assert res.induced_templates == 2
        assert res.grouping_accuracy == 1.0
        assert res.template_count_ratio == 1.0
        assert res.to_dict()["system"] == "Synthetic"
