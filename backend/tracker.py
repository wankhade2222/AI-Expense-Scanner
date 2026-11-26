import csv
import io
import os
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from babel.numbers import get_currency_precision
from pydantic import BaseModel, ConfigDict

CATEGORIES = ("Software", "Office supplies", "Travel", "Meals", "Professional services", "Utilities", "Uncategorized")
WORKSPACE_TIMEZONE = ZoneInfo(os.getenv("WORKSPACE_TIMEZONE") or "Asia/Kolkata")
CATEGORY_RULES = (
    ("Software", r"software|hosting|adobe|github|domain|saas"),
    ("Office supplies", r"stationery|paper|printing|office supplies"),
    ("Travel", r"taxi|fuel|rail|airline|parking|transit"),
    ("Meals", r"cafe|coffee|restaurant|catering|lunch"),
    ("Professional services", r"accounting|consulting|legal|bookkeeping"),
    ("Utilities", r"electricity|internet|broadband|water bill"),
)


class TrackerExpense(BaseModel):
    receipt_id: str
    merchant: str
    document_date: date
    invoice_number: str | None
    due_date: date | None
    paid_on: date | None
    status: Literal["paid", "unpaid"]
    category: str
    currency: str
    total_minor: str
    amount: str
    amount_breakdown: dict[str, str]
    overdue: bool


class TrackerTotal(BaseModel):
    currency: str
    category: str | None
    total_minor: str
    amount: str


class ExpenseReport(BaseModel):
    expenses: list[TrackerExpense]
    totals: list[TrackerTotal]
    category_totals: list[TrackerTotal]
    categories: tuple[str, ...] = CATEGORIES
    today: date
    month: str | None
    next_day_at: datetime


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paid_on: str
    prepayment: bool = False


class PaymentCorrectionRequest(PaymentRequest):
    previous_paid_on: str


class CategoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: str


def workspace_today(now: datetime | None = None) -> date:
    return now.astimezone(WORKSPACE_TIMEZONE).date() if now is not None else datetime.now(WORKSPACE_TIMEZONE).date()


def tracker_category(fields: dict) -> str:
    details = " ".join([fields["merchant"] or "", *(item["description"] or "" for item in fields["line_items"])])
    for name, pattern in CATEGORY_RULES:
        if re.search(rf"\b(?:{pattern})\b", details, re.IGNORECASE):
            return name
    return CATEGORIES[-1]


def amount_text(amount: int, currency: str) -> str:
    precision = get_currency_precision(currency)
    whole, fraction = divmod(abs(amount), 10**precision)
    magnitude = f"{whole}.{fraction:0{precision}d}" if precision else str(whole)
    return "-" + magnitude if amount < 0 else magnitude


def effective_paid_on(fields: dict, paid_on: date | None) -> date | None:
    if paid_on is None and fields["payment_status"] == "paid":
        return date.fromisoformat(fields["payment_date"])
    return paid_on


def tracker_expense(
    receipt_id: str, fields: dict, currency: str, amounts_minor: dict[str, int],
    *, paid_on: date | None, category: str | None, today: date,
) -> TrackerExpense:
    paid_on = effective_paid_on(fields, paid_on)
    try:
        due_date = date.fromisoformat(fields["due_date"] or "")
    except ValueError:
        due_date = None
    amount = amounts_minor["total"]
    return TrackerExpense(
        receipt_id=receipt_id, merchant=fields["merchant"], document_date=fields["document_date"],
        invoice_number=fields["invoice_number"], due_date=due_date, paid_on=paid_on,
        status="paid" if paid_on is not None else "unpaid", category=category or tracker_category(fields),
        currency=currency, total_minor=str(amount), amount=amount_text(amount, currency),
        amount_breakdown={name: amount_text(amounts_minor[name], currency)
                          for name in ("subtotal", "discount", "tax", "fees", "rounding") if name in amounts_minor},
        overdue=paid_on is None and due_date is not None and due_date < today,
    )


def summarize(expenses: list[TrackerExpense], *, today: date, month: str | None) -> ExpenseReport:
    totals: dict[str, int] = defaultdict(int)
    categories: dict[tuple[str, str], int] = defaultdict(int)
    for expense in expenses:
        amount = int(expense.total_minor)
        totals[expense.currency] += amount
        categories[(expense.currency, expense.category)] += amount
    return ExpenseReport(
        expenses=expenses,
        today=today, month=month,
        next_day_at=datetime.combine(today + timedelta(days=1), time.min, tzinfo=WORKSPACE_TIMEZONE),
        totals=[TrackerTotal(currency=currency, category=None, total_minor=str(total), amount=amount_text(total, currency))
                for currency, total in sorted(totals.items())],
        category_totals=[TrackerTotal(currency=currency, category=category, total_minor=str(total), amount=amount_text(total, currency))
                         for (currency, category), total in sorted(categories.items())],
    )


def spreadsheet_text(value: str | None) -> str:
    text = value or ""
    if text.startswith(("\t", "\n", "\r")) or text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def expense_csv(expenses: list[TrackerExpense]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(("Merchant", "Invoice number", "Category", "Currency", "Total", "Status",
                     "Document date", "Due date", "Paid on", "Subtotal", "Discount", "Tax", "Fees", "Rounding"))
    for expense in expenses:
        writer.writerow((
            spreadsheet_text(expense.merchant), spreadsheet_text(expense.invoice_number), expense.category,
            expense.currency, expense.amount, expense.status, expense.document_date, expense.due_date, expense.paid_on,
            *(expense.amount_breakdown.get(name) for name in ("subtotal", "discount", "tax", "fees", "rounding")),
        ))
    return output.getvalue()
