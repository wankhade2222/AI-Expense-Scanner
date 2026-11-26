import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_resume_reuses_only_scored_cases_with_matching_versions(self):
        clean = evaluate.evaluate(evaluate.CASES[0], None, evaluate.VALIDATION_DATE)
        failed = {"id": evaluate.CASES[1].case_id, "error": "Service unavailable", "http_status": 429}
        report = evaluate.build_report([clean, failed], None, evaluate.VALIDATION_DATE)
        self.assertEqual(evaluate.resumable_cases(report, None, evaluate.VALIDATION_DATE), {clean["id"]: clean})
        with self.assertRaisesRegex(ValueError, "prompt_version"):
            evaluate.resumable_cases({**report, "prompt_version": "other"}, None, evaluate.VALIDATION_DATE)

    def test_failed_comparison_saves_a_resumable_partial_run(self):
        config = evaluate.ExtractionConfig("gemini", "fixture-live", "test-only")
        first, second, other = evaluate.CASES[:3]
        scored = evaluate.evaluate(first, None, evaluate.VALIDATION_DATE)
        failure = {"id": second.case_id, "error": "Quota exhausted", "http_status": 429}
        previous = evaluate.build_report(
            [evaluate.evaluate(other, None, evaluate.VALIDATION_DATE)], config, evaluate.VALIDATION_DATE,
        )
        with TemporaryDirectory() as directory, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            comparison_path = Path(directory) / "previous.json"
            output_path = Path(directory) / "current.json"
            comparison_path.write_text(json.dumps(previous), encoding="utf-8")
            with patch.object(sys, "argv", [
                "evaluation", "--live", "--limit", "2", "--compare", str(comparison_path), "--output", str(output_path),
            ]), patch.object(evaluate, "extraction_config", return_value=config), patch.object(
                evaluate, "evaluate", side_effect=lambda case, *args, **kwargs: scored if case == first else failure,
            ):
                self.assertEqual(evaluate.main(), 2)
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["coverage"], {"requested": 2, "scored": 1})
            self.assertEqual(saved["cases"], [scored, failure])
            self.assertIn("No successfully scored documents overlap", saved["comparison_error"])
            self.assertIn(saved["comparison_error"], stderr.getvalue())

            recovered = evaluate.evaluate(second, None, evaluate.VALIDATION_DATE)
            with patch.object(sys, "argv", [
                "evaluation", "--live", "--limit", "2", "--resume", str(output_path), "--output", str(output_path),
            ]), patch.object(evaluate, "extraction_config", return_value=config), patch.object(
                evaluate, "evaluate", return_value=recovered,
            ) as evaluator:
                self.assertEqual(evaluate.main(), 0)
            evaluator.assert_called_once()
            self.assertEqual(evaluator.call_args.args[0], second)
            resumed = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(resumed["cases"], [scored, recovered])
            self.assertNotIn("comparison_error", resumed)

    def test_model_input_change_invalidates_comparison_and_resume(self):
        version, images = evaluate.rendered_dataset()
        self.assertEqual(evaluate.dataset_fingerprint(evaluate.CASES, images), version)
        case = next(case for case in evaluate.CASES if case.sample.obscure_total)
        changed_images = {**images, case.case_id: [images[case.case_id][0] + b"changed-render"]}
        changed_version = evaluate.dataset_fingerprint(evaluate.CASES, changed_images)
        self.assertNotEqual(changed_version, version)
        scored = evaluate.evaluate(case, None, evaluate.VALIDATION_DATE)
        current = evaluate.build_report([scored], None, evaluate.VALIDATION_DATE)
        previous = {**current, "dataset_version": changed_version}
        with self.assertRaisesRegex(ValueError, "dataset_version"):
            evaluate.compare_reports(current, previous)
        with self.assertRaisesRegex(ValueError, "dataset_version"):
            evaluate.resumable_cases(previous, None, evaluate.VALIDATION_DATE)
