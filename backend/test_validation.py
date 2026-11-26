from datetime import date
import unittest

from backend.extraction import ReceiptExtraction
from backend.validation import duplicate_fingerprint, parse_minor, validate_receipt


def invoice(**overrides):
    fields = dict(
        merchant="Paper Fern", document_type="invoice", document_date="2026-11-26",
        invoice_number="INV-42", due_date="2026-12-10", payment_status="unpaid",
        payment_date=None, currency="INR", gstin=None, line_items=[],
        subtotal="1,200.00", discount=None, tax="216.00", fees=None,
        rounding=None, total="1,416.00", sources=[],
    )
    fields.update(overrides)
    return ReceiptExtraction.model_validate(fields)


class ValidationTests(unittest.TestCase):
    def report(self, **overrides):
        return validate_receipt(invoice(**overrides), today=date(2026, 11, 26))

    def test_valid_tax_inclusive_total_and_future_due_date(self):
        report = self.report(total="1,200.00")
        self.assertEqual(report.totals_check, "matched")
        self.assertEqual(report.flags, [])
        self.assertEqual(report.amounts_minor["total"], 120_000)

    def test_wrong_total_has_source_and_difference(self):
        report = self.report(total="1,500.00", sources=[{"field": "total", "page": 2, "box": None}])
        self.assertEqual(report.totals_check, "mismatch")
        flag, = report.flags
        self.assertEqual((flag.code, flag.field, flag.source.page), ("totals_mismatch", "total", 2))
        self.assertIn("INR 84.00", flag.reason)

    def test_partial_lines_and_invalid_tax_leave_total_unevaluated(self):
        report = self.report(
            subtotal=None, tax="unreadable",
            line_items=[{"description": "Paper", "qty": None, "amount": None, "unit_price": None}],
        )
        self.assertEqual(report.totals_check, "not_evaluated")
        self.assertEqual({flag.field for flag in report.flags}, {"line_items.0.amount", "tax"})

    def test_discount_fees_rounding_and_complete_lines(self):
        report = self.report(
            subtotal=None, tax="10", discount="-15", fees="4.50", rounding="-0.01", total="99.49",
            line_items=[{"description": "Paper", "qty": None, "amount": "100.00", "unit_price": None}],
        )
        self.assertEqual(report.totals_check, "matched")

    def test_subtotal_can_already_include_discount_when_line_items_support_it(self):
        report = self.report(
            subtotal="90", tax="5", discount="10", total="95",
            line_items=[{"description": "Paper", "qty": "1", "amount": "100", "unit_price": "100"}],
        )
        self.assertEqual(report.totals_check, "matched")

    def test_paid_requires_payment_date_and_unknown_requires_confirmation(self):
        self.assertTrue(any(flag.field == "payment_date" for flag in self.report(payment_status="paid").flags))
        self.assertTrue(any(flag.code == "future_date" for flag in self.report(
            payment_status="paid", payment_date="2026-11-27",
        ).flags))
        self.assertTrue(any(flag.code == "payment_status_conflict" for flag in self.report(
            payment_status="unpaid", payment_date="2026-11-26",
        ).flags))
        self.assertTrue(any(flag.code == "unknown_payment_status" and flag.severity == "blocking"
                            for flag in self.report(payment_status="unknown").flags))

    def test_format_and_missing_flags(self):
        report = self.report(merchant=None, currency="QQQ", document_date="2026-02-30", gstin="incorrect", total=None)
        self.assertEqual(
            {(flag.field, flag.code) for flag in report.flags},
            {("merchant", "missing"), ("currency", "invalid_currency"), ("document_date", "invalid_date"),
             ("gstin", "invalid_gstin_format"), ("total", "missing")},
        )

    def test_printed_amounts_use_currency_precision(self):
        for printed, currency, expected in (
            ("₹1,23,456.78", "INR", 12_345_678), ("1,000", "INR", 100_000),
            ("1.234,50 EUR", "EUR", 123_450), ("JPY 1,010", "JPY", 1_010),
            ("BHD 1234.567", "BHD", 1_234_567), ("(0.01)", "USD", -1),
        ):
            with self.subTest(printed=printed):
                self.assertEqual(parse_minor(printed, currency), expected)
        with self.assertRaises(ValueError):
            parse_minor("BHD 12.345", "BHD")
        for printed in ("10,2,2", "EUR 10", "1e3", "10.14.44"):
            with self.assertRaises(ValueError):
                parse_minor(printed, "INR")

    def test_fingerprint_normalizes_merchant_and_amount(self):
        self.assertEqual(
            duplicate_fingerprint(invoice(), self.report()),
            duplicate_fingerprint(invoice(merchant=" PAPER   FERN ", total="1416.0"),
                                  self.report(merchant=" PAPER   FERN ", total="1416.0")),
        )
