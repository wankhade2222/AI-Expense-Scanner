import unittest

from backend import evaluation as evaluate
from backend.validation import ValidationFlag, validate_receipt


class EvaluationTests(unittest.TestCase):
    def test_offline_suite_exercises_flag_metrics_and_labels(self):
        cases = [evaluate.evaluate(case, None, evaluate.VALIDATION_DATE) for case in evaluate.CASES]
        report = evaluate.build_report(cases, None, evaluate.VALIDATION_DATE)
        self.assertEqual(report["coverage"], {"requested": 23, "scored": 23})
        self.assertEqual(report["fields"]["merchant"]["missed_errors"], 1)
        self.assertEqual(report["fields"]["total"]["correct"], 22)
        self.assertEqual(report["fields"]["total"]["missed_errors"], 0)
        self.assertEqual(report["fields"]["payment_status"]["unnecessary_flags"], 0)
        self.assertEqual(report["fields"]["payment_status"]["missed_expected_flags"], 0)
        self.assertEqual(cases[-3]["totals_check"], "matched")
        self.assertEqual(cases[-2]["fields"]["total"], {
            "expected": None, "actual": None, "correct": True, "flagged": True, "review_expected": True,
        })
        self.assertEqual(cases[-1]["totals_check"], "mismatch")
        self.assertEqual(report["model_versions"], ["fixture-v1"])
        self.assertEqual(report["prompt_version"], evaluate.PROMPT_VERSION)
        self.assertEqual(report["rules_version"], evaluate.RULES_VERSION)

    def test_expected_and_unnecessary_flags_are_counted_separately(self):
        case = evaluate.CASES[-2]
        fields = evaluate.fixture_fields(case)
        report = validate_receipt(fields, today=evaluate.VALIDATION_DATE)
        report.flags = [ValidationFlag(field="merchant", code="fixture", reason="Fixture flag", severity="warning")]
        scored = evaluate.score_case(case, fields, report)
        metrics = evaluate.summarize_cases([scored])
        self.assertEqual(metrics["merchant"]["unnecessary_flags"], 1)
        self.assertEqual(metrics["total"]["missed_expected_flags"], 1)
        self.assertEqual(metrics["total"]["missed_errors"], 0)

    def test_equivalent_amounts_match_and_failed_documents_are_excluded(self):
        case = evaluate.CASES[5]
        fields = evaluate.fixture_fields(case).model_copy(update={"total": "1150"})
        scored = evaluate.score_case(case, fields, validate_receipt(fields, today=evaluate.VALIDATION_DATE))
        report = evaluate.build_report([{"id": case.case_id, "model": "fixture-v1", **scored},
                                        {"id": "failed", "error": "Service unavailable"}], None, evaluate.VALIDATION_DATE)
        self.assertEqual(report["coverage"], {"requested": 2, "scored": 1})
        self.assertEqual(report["fields"]["total"]["labelled"], 1)
        self.assertEqual(report["fields"]["total"]["accuracy"], 1)

    def test_comparison_scores_common_documents_and_rejects_another_dataset(self):
        clean = evaluate.evaluate(evaluate.CASES[0], None, evaluate.VALIDATION_DATE)
        faulty = evaluate.evaluate(evaluate.CASES[-1], None, evaluate.VALIDATION_DATE)
        case = evaluate.CASES[-1]
        corrected = evaluate.evaluate(evaluate.Case(case.sample), None, evaluate.VALIDATION_DATE)
        old = evaluate.build_report([clean, faulty], None, evaluate.VALIDATION_DATE)
        new = evaluate.build_report([corrected], None, evaluate.VALIDATION_DATE)
        comparison = evaluate.compare_reports(new, old)
        self.assertEqual(comparison["overlapping_cases"], 1)
        self.assertEqual(comparison["field_deltas"]["merchant"]["accuracy"], 1)
        self.assertEqual(comparison["field_deltas"]["merchant"]["missed_errors"], -1)
        with self.assertRaises(ValueError):
            evaluate.compare_reports(new, {**old, "dataset_version": "other"})
