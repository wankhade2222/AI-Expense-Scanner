"""Run the M2 extraction baseline on synthetic documents, with real API calls."""

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from backend.extraction import ExtractionError, extract_receipt
from backend.main import extraction_pages


@dataclass(frozen=True)
class Sample:
    merchant: str
    document_date: str
    currency: str
    total: str
    payment_status: str
    due_date: str | None = None
    pages: int = 1

    @property
    def document_type(self):
        return "invoice" if self.due_date else "receipt"


SAMPLES = (
    Sample("Paper Fern", "2026-09-01", "INR", "540.00", "paid"),
    Sample("Indigo Stationery", "2026-09-02", "INR", "184.50", "paid"),
    Sample("Oak Studio", "2026-09-03", "USD", "25.00", "paid"),
    Sample("Cedar Printing", "2026-09-04", "EUR", "49.95", "paid"),
    Sample("Maple Office", "2026-09-05", "GBP", "17.50", "paid"),
    Sample("Slate Tools", "2026-09-06", "INR", "1,150.00", "paid"),
    Sample("Harbor Copy", "2026-09-07", "USD", "12.60", "paid"),
    Sample("Elm Couriers", "2026-09-08", "EUR", "11.00", "paid"),
    Sample("Mango Press", "2026-09-09", "INR", "85.00", "paid"),
    Sample("Willow Supply", "2026-09-10", "GBP", "107.10", "paid"),
    Sample("West Design Supply", "2026-09-11", "INR", "3,700.00", "unpaid", "2026-10-11", 2),
    Sample("Birch Studio Supply", "2026-09-12", "USD", "600.25", "unpaid", "2026-10-12"),
    Sample("Saffron Paper", "2026-09-13", "INR", "2,250.00", "unknown", "2026-10-13"),
    Sample("Pine Accounting", "2026-09-14", "EUR", "950.00", "unpaid", "2026-10-14", 2),
    Sample("Stone Equipment", "2026-09-15", "GBP", "181.20", "unknown", "2026-10-15"),
    Sample("Amber Wholesale", "2026-09-16", "INR", "7,900.75", "unpaid", "2026-10-16", 2),
    Sample("Rose Transit", "2026-09-17", "USD", "280.00", "paid", "2026-10-17"),
    Sample("Blue Ledger", "2026-09-18", "EUR", "100.00", "unknown", "2026-10-18"),
    Sample("Juniper Media", "2026-09-19", "GBP", "470.99", "paid", "2026-10-19"),
    Sample("Banyan Workspace", "2026-09-20", "INR", "5,000.00", "unpaid", "2026-10-20", 2),
)


def render_sample(sample: Sample) -> tuple[bytes, str]:
    font = ImageFont.load_default(size=29)
    images = []
    for number in range(1, sample.pages + 1):
        image = Image.new("RGB", (900, 480), "white")
        draw = ImageDraw.Draw(image)
        lines = [sample.merchant, sample.document_type.upper(), f"Page {number} of {sample.pages}"]
        if number == 1:
            lines += [f"Document date: {sample.document_date}", f"Currency: {sample.currency}"]
            if sample.due_date:
                lines.extend(["Invoice number: T-" + sample.document_date[-2:], f"Due date: {sample.due_date}"])
        if number == sample.pages:
            lines += [f"Total: {sample.total}"]
            if sample.payment_status != "unknown":
                lines.append(f"Payment status: {sample.payment_status.upper()}")
            if sample.payment_status == "paid":
                lines.append(f"Payment date: {sample.document_date}")
        draw.multiline_text((48, 35), "\n".join(lines), font=font, fill="black", spacing=11)
        images.append(image)
    target = io.BytesIO()
    if sample.document_type == "invoice":
        images[0].save(target, "PDF", save_all=True, append_images=images[1:])
        return target.getvalue(), "application/pdf"
    images[0].save(target, "JPEG")
    return target.getvalue(), "image/jpeg"


def main() -> None:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise SystemExit("Set OPENAI_API_KEY to run the live sample baseline.")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    mismatches = 0
    for sample in SAMPLES:
        try:
            fields = extract_receipt(extraction_pages(*render_sample(sample)), api_key=key, model=model).fields
        except ExtractionError as exc:
            raise SystemExit(f"{sample.merchant}: {exc}") from exc
        names = ("merchant", "document_type", "document_date", "currency", "total", "payment_status", "due_date")
        differences = [name for name in names if getattr(fields, name) != getattr(sample, name)]
        mismatches += bool(differences)
        print(f"{sample.merchant}: " + (", ".join(differences) if differences else "matched"))
    print(f"{len(SAMPLES) - mismatches}/{len(SAMPLES)} documents matched all labelled fields.")
    raise SystemExit(bool(mismatches))


if __name__ == "__main__":
    main()
