import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Literal

from babel.numbers import get_currency_precision, is_currency
from pydantic import BaseModel

from backend.extraction import ReceiptExtraction, Source

RULES_VERSION = "receipt-rules-v1"
AMOUNT_FIELDS = ("subtotal", "discount", "tax", "fees", "rounding", "total")
REQUIRED = ("merchant", "document_type", "document_date", "currency", "total")
GSTIN = re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]")
DIGITS = re.compile(r"[0-9]+")
GROUPED = re.compile(r"[0-9]{1,3}(?:[ ,.'’][0-9]{3})+")
INDIAN_GROUPED = re.compile(r"[0-9]{1,2}(?:,[0-9]{2})+,[0-9]{3}")
CURRENCY_MARKS = re.compile(r"^(?:[A-Z]{3}|RS\.?|[₹$€£¥])\s*|\s*(?:[A-Z]{3}|RS\.?|[₹$€£¥])$", re.IGNORECASE)


class ValidationFlag(BaseModel):
    field: str
    code: str
    severity: Literal["blocking", "warning"]
    reason: str
    source: Source | None = None


class ValidationReport(BaseModel):
    rules_version: str = RULES_VERSION
    currency: str | None
    amounts_minor: dict[str, int]
    totals_check: Literal["matched", "mismatch", "not_evaluated"]
    flags: list[ValidationFlag]


def whole_digits(text: str) -> str | None:
    if DIGITS.fullmatch(text):
        return text
    # Refuse inconsistent grouping separators within a single amount.
    separators = set(re.findall(r"[^0-9]", text))
    if len(separators) == 1 and (GROUPED.fullmatch(text) or INDIAN_GROUPED.fullmatch(text)):
        return re.sub(r"[^0-9]", "", text)
    return None


def parse_minor(value: str, currency: str) -> int:
    text = unicodedata.normalize("NFKC", value).strip()
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    if text.startswith(("-", "−", "+")):
        if negative:
            raise ValueError("Ambiguous sign")
        negative = text[0] != "+"
        text = text[1:].strip()
    # Read the printed currency marker without silently overriding the currency field.
    marks = CURRENCY_MARKS.findall(text)
    if len(marks) > 1:
        raise ValueError("Multiple currency markers")
    if marks:
        mark = marks[0].strip().upper().rstrip(".")
        explicit = {"₹": "INR", "RS": "INR", "€": "EUR", "£": "GBP"}.get(mark, mark)
        if explicit not in (currency, "$", "¥"):
            raise ValueError("Conflicting currency marker")
        text = CURRENCY_MARKS.sub("", text).strip()
    if len(text) > 32:
        raise ValueError("Amount is too long")

    precision = get_currency_precision(currency)
    values = set()
    whole = whole_digits(text)
    if whole is not None:
        values.add(int(whole) * 10**precision)
    for separator in (".", ","):
        if text.count(separator) != 1:
            continue
        whole_text, fraction = text.split(separator)
        whole = whole_digits(whole_text)
        if whole is not None and DIGITS.fullmatch(fraction):
            significant = fraction.rstrip("0")
            if len(fraction) <= precision or (not values and len(significant) <= precision):
                values.add(int(whole) * 10**precision + int(significant.ljust(precision, "0") or "0"))
    if len(values) != 1:
        raise ValueError("Invalid or ambiguous amount")
    amount = values.pop()
    if amount >= 10**16:
        raise ValueError("Amount is out of range")
    return -amount if negative else amount


def parse_date(text: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        raise ValueError("Expected YYYY-MM-DD")
    return date.fromisoformat(text)


def display_minor(amount: int, currency: str) -> str:
    precision = get_currency_precision(currency)
    whole, fraction = divmod(amount, 10**precision)
    formatted = f"{whole}.{fraction:0{precision}d}" if precision else str(whole)
    return f"{currency} {formatted}"


def duplicate_fingerprint(fields: ReceiptExtraction, report: ValidationReport) -> str | None:
    merchant = " ".join(unicodedata.normalize("NFKC", fields.merchant or "").casefold().split())
    total = report.amounts_minor.get("total")
    if not merchant or not fields.document_date or report.currency is None or total is None:
        return None
    if any(flag.severity == "blocking" and flag.field in ("document_date", "total") for flag in report.flags):
        return None
    key = [merchant, fields.document_date, report.currency, total]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


def invoice_identity(invoice_number: str | None) -> str | None:
    return unicodedata.normalize("NFKC", invoice_number or "").strip().casefold() or None


def validate_receipt(fields: ReceiptExtraction, *, today: date) -> ValidationReport:
    flags = []
    sources = {source.field: source for source in fields.sources}

    def flag(field, code, reason, severity="warning"):
        flags.append(ValidationFlag(field=field, code=code, reason=reason, severity=severity, source=sources.get(field)))

    for name in REQUIRED:
        value = getattr(fields, name)
        if not value or not value.strip():
            flag(name, "missing", f"Enter the {name.replace('_', ' ')}.", "blocking")

    for name in ("document_date", "due_date", "payment_date"):
        value = getattr(fields, name)
        if not value or not value.strip():
            continue
        required = name == "document_date" or (name == "payment_date" and fields.payment_status == "paid")
        severity = "blocking" if required else "warning"
        try:
            value_date = parse_date(value)
        except ValueError:
            flag(name, "invalid_date", "Enter a valid date in YYYY-MM-DD format.", severity)
            continue
        if value_date > today and required:
            flag(name, "future_date", "This date is in the future. Check it against the document.", severity)

    if fields.payment_status == "unknown":
        flag("payment_status", "unknown_payment_status", "Confirm whether this document has been paid.", "blocking")
    elif fields.payment_status == "paid" and not (fields.payment_date or "").strip():
        flag("payment_date", "missing", "Enter the date the payment was made.", "blocking")
    elif fields.payment_status == "unpaid" and (fields.payment_date or "").strip():
        flag("payment_status", "payment_status_conflict", "The document is marked unpaid but has a payment date.", "blocking")

    if fields.gstin and fields.gstin.strip() and not GSTIN.fullmatch(fields.gstin.strip().upper()):
        flag("gstin", "invalid_gstin_format", "Check the supplier GSTIN against the document.")

    currency = (fields.currency or "").strip().upper() or None
    if currency and (not re.fullmatch(r"[A-Z]{3}", currency) or not is_currency(currency) or currency in {"XXX", "XTS"}):
        flag("currency", "invalid_currency", "Enter a recognized currency code.", "blocking")
        currency = None

    amounts = {}
    printed = {name: getattr(fields, name) for name in AMOUNT_FIELDS}
    for index, item in enumerate(fields.line_items):
        printed[f"line_items.{index}.amount"] = item.amount
        printed[f"line_items.{index}.unit_price"] = item.unit_price
        if not (item.amount or "").strip():
            flag(f"line_items.{index}.amount", "missing", f"Check the amount on line item {index + 1}.")
    if currency:
        for name, value in printed.items():
            if not value or not value.strip():
                continue
            try:
                amount = parse_minor(value, currency)
                if name == "total" and amount < 0:
                    raise ValueError("Negative expense total")
                amounts[name] = amount
            except ValueError:
                flag(name, "invalid_amount", "Check the printed amount and its currency.", "blocking" if name == "total" else "warning")

    totals_check = "not_evaluated"
    subtotal = amounts.get("subtotal")
    item_keys = [f"line_items.{index}.amount" for index in range(len(fields.line_items))]
    line_sum = sum(amounts[name] for name in item_keys) if item_keys and all(name in amounts for name in item_keys) else None
    if not (fields.subtotal or "").strip():
        subtotal = line_sum
    components = ("tax", "discount", "fees", "rounding")
    if subtotal is not None and "total" in amounts and all(not (printed[name] or "").strip() or name in amounts for name in components):
        discount = abs(amounts.get("discount", 0))
        if line_sum is not None and line_sum - subtotal == discount:
            discount = 0
        base = subtotal - discount + amounts.get("fees", 0) + amounts.get("rounding", 0)
        expected = (base, base + amounts.get("tax", 0))
        delta = min(abs(amounts["total"] - candidate) for candidate in expected)
        totals_check = "matched" if delta <= 1 else "mismatch"
        if totals_check == "mismatch":
            flag("total", "totals_mismatch", f"The printed total differs by at least {display_minor(delta, currency)} from the calculated totals.")

    return ValidationReport(currency=currency, amounts_minor=amounts, totals_check=totals_check, flags=flags)
