"""Evaluate extraction and validation on labelled synthetic receipts and invoices."""

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from time import sleep
import unicodedata

import httpx

from backend.extraction import PROMPT_VERSION, ExtractionConfig, ExtractionError, ReceiptExtraction, extract_receipt, extraction_config
from backend.extraction_samples import SAMPLES, Sample, render_sample
from backend.main import extraction_pages
from backend.validation import AMOUNT_FIELDS, RULES_VERSION, ValidationReport, parse_minor, validate_receipt

VALIDATION_DATE = date(2026, 11, 26)
REPORT_VERSION = 1


@dataclass(frozen=True)
class Case:
    sample: Sample
    fixture_overrides: dict = field(default_factory=dict)

    @property
    def case_id(self) -> str:
        return self.sample.merchant.lower().replace(" ", "-")

    def labels(self) -> dict:
        sample = self.sample
        labels = {
            "merchant": sample.merchant, "document_type": sample.document_type,
            "document_date": sample.document_date, "currency": sample.currency,
            "total": None if sample.obscure_total else sample.total,
            "payment_status": sample.payment_status, "due_date": sample.due_date,
            "payment_date": sample.document_date if sample.payment_status == "paid" else None,
            "invoice_number": "T-" + sample.document_date[-2:] if sample.due_date else None,
        }
        if sample.subtotal is not None:
            labels["subtotal"] = sample.subtotal
        if sample.tax is not None:
            labels["tax"] = sample.tax
        return labels

    def expected_review(self) -> set[str]:
        result = set()
        if self.sample.payment_status == "unknown":
            result.add("payment_status")
        if self.sample.obscure_total:
            result.add("total")
        return result


CASES = tuple(Case(sample) for sample in SAMPLES) + (
    Case(Sample("Inclusive Stationery", "2026-09-21", "INR", "100.00", "paid",
                subtotal="100.00", tax="18.00", tax_included=True)),
    Case(Sample("Masked Total", "2026-09-22", "INR", "540.00", "paid", obscure_total=True)),
    Case(Sample("Poppy Office", "2026-09-23", "INR", "118.00", "paid", subtotal="100.00", tax="18.00"),
         {"total": "130.00", "merchant": "Poppy Supply"}),
)


def dataset_fingerprint(cases: tuple[Case, ...], images: dict[str, list[bytes]]) -> str:
    manifest = [{
        "definition": asdict(case),
        "labels": case.labels(),
        "expected_review": sorted(case.expected_review()),
        "page_hashes": [hashlib.sha256(page).hexdigest() for page in images[case.case_id]],
    } for case in cases]
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return "synthetic-receipts-v2-" + fingerprint


@lru_cache(maxsize=1)
def rendered_dataset() -> tuple[str, dict[str, list[bytes]]]:
    # Hash the exact JPEG inputs. PDF container metadata can vary even when rendered pages are identical.
    images = {case.case_id: extraction_pages(*render_sample(case.sample)) for case in CASES}
    return dataset_fingerprint(CASES, images), images


def fixture_fields(case: Case) -> ReceiptExtraction:
    payload = dict.fromkeys(ReceiptExtraction.model_fields)
    payload.update(case.labels(), line_items=[], sources=[])
    payload.update(case.fixture_overrides)
    return ReceiptExtraction.model_validate(payload)


def normalized(field: str, value: str | None, currency: str) -> str | int | None:
    if value is None or not value.strip():
        return None
    text = unicodedata.normalize("NFKC", value.strip())
    if field in AMOUNT_FIELDS:
        try:
            return parse_minor(text, currency)
        except ValueError:
            return text
    if field == "merchant":
        return " ".join(text.casefold().split())
    if field == "currency":
        return text.upper()
    return text


def score_case(case: Case, fields: ReceiptExtraction, report: ValidationReport) -> dict:
    flagged = {flag.field for flag in report.flags}
    expected_review = case.expected_review()
    currency = case.sample.currency
    observed = fields.model_dump()
    results = {}
    for name, expected in case.labels().items():
        actual = observed[name]
        results[name] = {
            "expected": expected, "actual": actual,
            "correct": normalized(name, expected, currency) == normalized(name, actual, currency),
            "flagged": name in flagged, "review_expected": name in expected_review,
        }
    return {
        "fields": results,
        "flag_codes": [flag.model_dump(include={"field", "code", "severity"}) for flag in report.flags],
        "totals_check": report.totals_check,
    }


def summarize_cases(cases: list[dict]) -> dict:
    fields: dict[str, Counter] = defaultdict(Counter)
    for case in cases:
        for name, result in case.get("fields", {}).items():
            counts = fields[name]
            correct, flagged, expected = result["correct"], result["flagged"], result["review_expected"]
            counts["labelled"] += 1
            counts["correct"] += correct
            counts["missed_errors"] += not correct and not flagged
            counts["unnecessary_flags"] += correct and flagged and not expected
            counts["missed_expected_flags"] += correct and expected and not flagged
    return {
        name: {**counts, "accuracy": counts["correct"] / counts["labelled"]}
        for name, counts in sorted(fields.items())
    }


def transient_error(error: ExtractionError) -> bool:
    cause = error.__cause__
    return isinstance(cause, httpx.RequestError) or (
        isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code in {500, 502, 503, 504}
    )


def failure_details(error: ExtractionError) -> dict:
    cause = error.__cause__
    if not isinstance(cause, httpx.HTTPStatusError):
        return {}
    status = cause.response.status_code
    details = {"http_status": status}
    if status == 429:
        try:
            provider_details = cause.response.json().get("error", {}).get("details", [])
            ids = {violation["quotaId"] for item in provider_details for violation in item.get("violations", [])
                   if isinstance(violation.get("quotaId"), str)}
            if ids:
                details["quota_ids"] = sorted(ids)
        except (ValueError, TypeError, AttributeError):
            pass
    return details


def evaluate(
    case: Case, config: ExtractionConfig | None, validation_date: date, *, pages: list[bytes] | None = None,
) -> dict:
    result = {"id": case.case_id, "merchant": case.sample.merchant}
    if config is None:
        fields, model = fixture_fields(case), "fixture-v1"
    else:
        if pages is None:
            pages = extraction_pages(*render_sample(case.sample))
        for attempt in (1, 2):
            try:
                extraction = extract_receipt(pages, api_key=config.api_key, model=config.model, provider=config.provider)
                fields, model = extraction.fields, extraction.model
                result.update(attempts=attempt, latency_ms=extraction.latency_ms)
                break
            except ExtractionError as exc:
                if attempt == 1 and transient_error(exc):
                    sleep(3)
                else:
                    return {**result, "attempts": attempt, "error": str(exc), **failure_details(exc)}
    return {
        **result, "model": model,
        **score_case(case, fields, validate_receipt(fields, today=validation_date)),
    }


def build_report(cases: list[dict], config: ExtractionConfig | None, validation_date: date) -> dict:
    return {
        "report_version": REPORT_VERSION, "dataset_version": rendered_dataset()[0],
        "generated_at": datetime.now(timezone.utc).isoformat(), "validation_date": validation_date.isoformat(),
        "provider": config.provider if config else "fixture",
        "requested_model": config.model if config else "fixture-v1",
        "model_versions": sorted({case["model"] for case in cases if "model" in case}),
        "prompt_version": PROMPT_VERSION, "rules_version": RULES_VERSION,
        "coverage": {"requested": len(cases), "scored": sum("fields" in case for case in cases)},
        "fields": summarize_cases(cases), "cases": cases,
    }


def compare_reports(current: dict, previous: dict) -> dict:
    for key in ("report_version", "dataset_version", "validation_date"):
        if current[key] != previous[key]:
            raise ValueError(f"Reports differ in {key}. Compare runs with the same dataset and validation date.")
    prior = {case["id"]: case for case in previous["cases"] if "fields" in case}
    shared = [case for case in current["cases"] if "fields" in case and case["id"] in prior]
    if not shared:
        raise ValueError("No successfully scored documents overlap.")
    scores = summarize_cases(shared)
    old = summarize_cases([prior[case["id"]] for case in shared])
    keys = ("accuracy", "missed_errors", "unnecessary_flags", "missed_expected_flags")
    return {
        "previous_model": previous["requested_model"], "overlapping_cases": len(shared),
        "field_deltas": {name: {key: score[key] - old[name][key] for key in keys}
                         for name, score in scores.items()},
    }


def resumable_cases(previous: dict, config: ExtractionConfig | None, validation_date: date) -> dict[str, dict]:
    expected = build_report([], config, validation_date)
    for key in ("report_version", "dataset_version", "validation_date", "provider", "requested_model", "prompt_version", "rules_version"):
        if expected[key] != previous[key]:
            raise ValueError(f"Previous run differs in {key}. Use the same configuration or start a new run.")
    return {case["id"]: case for case in previous["cases"] if "fields" in case}


def print_report(report: dict) -> None:
    print(f"Provider: {report['provider']} | Models: {', '.join(report['model_versions']) or 'none returned'}")
    print(f"Prompt: {report['prompt_version']} | Rules: {report['rules_version']} | As of: {report['validation_date']}")
    coverage = report["coverage"]
    print(f"Coverage: {coverage['scored']}/{coverage['requested']} documents scored")
    print(f"{'Field':<19} {'Correct':>8} {'Accuracy':>9} {'Missed errors':>14} {'Extra flags':>12} {'Missed review':>14}")
    for name, scores in report["fields"].items():
        fraction = f"{scores['correct']}/{scores['labelled']}"
        print(f"{name:<19} {fraction:>8} {scores['accuracy']:>8.1%} {scores['missed_errors']:>14} "
              f"{scores['unnecessary_flags']:>12} {scores['missed_expected_flags']:>14}")
    if comparison := report.get("comparison"):
        print(f"Comparison with {comparison['previous_model']} over {comparison['overlapping_cases']} shared cases")
        for name, changes in comparison["field_deltas"].items():
            print(f"  {name}: {changes['accuracy'] * 100:+.1f} pp, "
                  f"{changes['missed_errors']:+d} missed errors, {changes['unnecessary_flags']:+d} extra flags, "
                  f"{changes['missed_expected_flags']:+d} missed reviews")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call the configured model. Default uses offline fixtures.")
    parser.add_argument("--limit", type=int, choices=range(1, len(CASES) + 1))
    parser.add_argument("--as-of", type=date.fromisoformat, default=VALIDATION_DATE)
    parser.add_argument("--output", type=Path, help="Write the detailed JSON report.")
    parser.add_argument("--compare", type=Path, help="Compare the metrics on overlapping cases from an earlier JSON report.")
    parser.add_argument("--resume", type=Path, help="Reuse successful cases from a compatible report and evaluate the remaining ones.")
    args = parser.parse_args()
    config = None
    if args.live:
        try:
            config = extraction_config()
        except ExtractionError as exc:
            parser.error(str(exc))
        if config is None:
            parser.error("--live requires a configured extraction provider API key.")
    scored = {}
    if args.resume:
        try:
            scored = resumable_cases(json.loads(args.resume.read_text(encoding="utf-8")), config, args.as_of)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(f"Could not resume the report. {exc}")
    selected = CASES[:args.limit]
    pending = [case for case in selected if case.case_id not in scored]
    _, images = rendered_dataset()
    if args.resume:
        print(f"Reusing {len(selected) - len(pending)} scored documents. Evaluating {len(pending)} remaining.")
    with ThreadPoolExecutor(max_workers=2 if args.live else 1) as executor:
        for result in executor.map(lambda case: evaluate(case, config, args.as_of, pages=images[case.case_id]), pending):
            scored[result["id"]] = result
            if args.live:
                quota = ", ".join(result.get("quota_ids", []))
                suffix = f" ({quota})" if quota else ""
                print(result["id"] + ": " + result.get("error", "scored") + suffix, flush=True)
    cases = [scored[case.case_id] for case in selected]
    report = build_report(cases, config, args.as_of)
    if args.compare:
        try:
            report["comparison"] = compare_reports(report, json.loads(args.compare.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            report["comparison_error"] = f"Could not compare reports. {exc}"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print_report(report)
    if comparison_error := report.get("comparison_error"):
        print(comparison_error, file=sys.stderr)
        return 2
    return int(report["coverage"]["scored"] < report["coverage"]["requested"])


if __name__ == "__main__":
    raise SystemExit(main())
