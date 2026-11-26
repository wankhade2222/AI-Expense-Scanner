import hashlib
import io
import logging
import os
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import uuid4

import pypdfium2 as pdfium
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Response, UploadFile, status
from PIL import Image, ImageOps
from pydantic import BaseModel
from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint, create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from backend.extraction import PROMPT_VERSION, ExtractionError, ReceiptExtraction, extract_receipt
from backend.validation import ValidationFlag, ValidationReport, duplicate_fingerprint, invoice_identity, validate_receipt

logger = logging.getLogger(__name__)
data_dir = Path(os.getenv("DATA_DIR", ".data")).resolve()
database_url = os.getenv("DATABASE_URL", f"sqlite:///{(data_dir / 'scanner.db').as_posix()}")
bucket = os.getenv("S3_BUCKET")
redis_url = os.getenv("REDIS_URL")
owner = "local-user"
max_bytes = 20 * 1024 * 1024
max_pixels = 30_000_000
max_pdf_pages = 30
pdf_lock = Lock()

engine = create_engine(database_url)


class Base(DeclarativeBase):
    pass


class Receipt(Base):
    __tablename__ = "receipts"
    __table_args__ = (UniqueConstraint("user_id", "sha256"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(80), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(40))
    storage_key: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[str] = mapped_column(String(32))
    page_count: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReceiptView(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    status: Literal["queued", "processing", "ready", "failed"]
    created_at: datetime
    page_count: int | None
    preview_url: str | None
    error: str | None


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    receipt_id: Mapped[str] = mapped_column(ForeignKey("receipts.id"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(32))
    fields: Mapped[dict] = mapped_column(JSON)
    raw_json: Mapped[dict] = mapped_column(JSON)
    latency_ms: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExtractionView(BaseModel):
    id: str
    model: str
    prompt_version: str
    latency_ms: int
    fields: ReceiptExtraction
    validation: ValidationReport


class SavedValidation(Base):
    __tablename__ = "extraction_validations"

    extraction_id: Mapped[str] = mapped_column(ForeignKey("extractions.id"), primary_key=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    invoice_identity: Mapped[str | None] = mapped_column(String)
    report: Mapped[dict] = mapped_column(JSON)


def validation_for(extraction_id: str, fields: ReceiptExtraction) -> SavedValidation:
    report = validate_receipt(fields, today=datetime.now(timezone.utc).date())
    return SavedValidation(
        extraction_id=extraction_id, fingerprint=duplicate_fingerprint(fields, report),
        invoice_identity=invoice_identity(fields.invoice_number) if fields.document_type == "invoice" else None,
        report=report.model_dump(mode="json"),
    )


def report_with_duplicates(session: Session, saved: SavedValidation, receipt: Receipt) -> ValidationReport:
    report = ValidationReport.model_validate(saved.report)
    if saved.fingerprint is None:
        return report
    candidate = (
        select(Receipt.id).join(SavedValidation, Receipt.attempt == SavedValidation.extraction_id)
        .where(Receipt.user_id == receipt.user_id, Receipt.id != receipt.id, Receipt.status == "ready",
               SavedValidation.fingerprint == saved.fingerprint)
    )
    if saved.invoice_identity is not None:
        candidate = candidate.where(
            (SavedValidation.invoice_identity == saved.invoice_identity) | SavedValidation.invoice_identity.is_(None),
        )
    if session.scalar(candidate.limit(1)) is not None:
        report.flags.append(ValidationFlag(
            field="document", code="possible_duplicate", severity="warning",
            reason="Another document matches the merchant, date, currency, and total. Check whether this is a separate transaction.",
        ))
    return report


@lru_cache
def s3_client():
    import boto3

    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None)


def store(key: str, data: bytes, content_type: str) -> None:
    if bucket:
        s3_client().put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return
    path = data_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load(key: str) -> bytes:
    if bucket:
        with s3_client().get_object(Bucket=bucket, Key=key)["Body"] as body:
            return body.read()
    return (data_dir / key).read_bytes()


def discard(key: str) -> None:
    if bucket:
        s3_client().delete_object(Bucket=bucket, Key=key)
    else:
        (data_dir / key).unlink(missing_ok=True)


def preview_key(receipt_id: str, attempt: str) -> str:
    return f"previews/{receipt_id}/{attempt}.jpg"


def prepare_preview(data: bytes, content_type: str) -> tuple[bytes, int]:
    if content_type == "application/pdf":
        with pdf_lock, pdfium.PdfDocument(data) as document:
            count = len(document)
            if not 1 <= count <= max_pdf_pages:
                raise ValueError("Upload a PDF with 1 to 30 pages.")
            with closing(document[0]) as page:
                with closing(page.render(scale=720 / max(page.get_size()))) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
    else:
        count = 1
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > max_pixels:
                raise ValueError("Upload a photo smaller than 30 megapixels.")
            image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((720, 720))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=85)
    return output.getvalue(), count


def extraction_pages(data: bytes, content_type: str) -> list[bytes]:
    def jpeg(image: Image.Image) -> bytes:
        image.thumbnail((1800, 1800))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=88)
        return buffer.getvalue()

    if content_type != "application/pdf":
        with Image.open(io.BytesIO(data)) as source:
            return [jpeg(ImageOps.exif_transpose(source).convert("RGB"))]
    pages = []
    with pdf_lock, pdfium.PdfDocument(data) as document:
        for page_index in range(len(document)):
            with closing(document[page_index]) as page:
                with closing(page.render(scale=1800 / max(page.get_size()))) as bitmap:
                    pages.append(jpeg(bitmap.to_pil().convert("RGB")))
    return pages


def process_receipt(receipt_id: str, attempt: str) -> None:
    with Session(engine) as session:
        receipt = session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.attempt == attempt, Receipt.status == "queued")
            .values(status="processing", updated_at=datetime.now(timezone.utc))
            .returning(Receipt)
        ).scalar_one_or_none()
        if receipt is None:
            return
        storage_key, content_type = receipt.storage_key, receipt.content_type
        session.commit()

    extracted = None
    count = None
    try:
        original = load(storage_key)
        preview, count = prepare_preview(original, content_type)
        store(preview_key(receipt_id, attempt), preview, "image/jpeg")
        if api_key := os.getenv("OPENAI_API_KEY"):
            extracted = extract_receipt(
                extraction_pages(original, content_type), api_key=api_key,
                model=os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
            )
        result = {"status": "ready", "page_count": count, "error": None}
    except ExtractionError as exc:
        result = {"status": "failed", "page_count": count, "error": str(exc)}
    except Exception as exc:
        logger.exception("Could not prepare receipt %s", receipt_id)
        error = str(exc) if type(exc) is ValueError else "Could not read this file. Retry or upload a fresh copy."
        result = {"status": "failed", "error": error}

    with Session(engine) as session:
        changed = session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.attempt == attempt, Receipt.status == "processing")
            .values(**result, updated_at=datetime.now(timezone.utc))
        ).rowcount
        if changed and extracted is not None:
            session.add(Extraction(
                id=attempt, receipt_id=receipt_id, model=extracted.model, prompt_version=PROMPT_VERSION,
                fields=extracted.fields.model_dump(), raw_json=extracted.raw_json,
                latency_ms=extracted.latency_ms, created_at=datetime.now(timezone.utc),
            ))
            session.flush()
            session.add(validation_for(attempt, extracted.fields))
        session.commit()


def mark_job_failed(job, connection, exc_type, exc_value, traceback) -> None:
    receipt_id, attempt = job.args
    with Session(engine) as session:
        session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.attempt == attempt, Receipt.status.in_(("queued", "processing")))
            .values(status="failed", error="Processing stopped. Retry to try again.")
        )
        session.commit()


def schedule(receipt_id: str, attempt: str) -> None:
    try:
        if redis_url:
            from redis import Redis
            from rq import Queue

            Queue("receipts", connection=Redis.from_url(redis_url)).enqueue(
                process_receipt, receipt_id, attempt, job_id=attempt, job_timeout=120,
                on_failure=mark_job_failed, on_stopped=mark_stopped_job,
            )
        else:
            process_receipt(receipt_id, attempt)
    except Exception:
        logger.exception("Could not start receipt %s", receipt_id)
        with Session(engine) as session:
            session.execute(
                update(Receipt)
                .where(Receipt.id == receipt_id, Receipt.attempt == attempt, Receipt.status == "queued")
                .values(status="failed", error="Could not start processing. Retry to try again.")
            )
            session.commit()


def mark_stopped_job(job, connection) -> None:
    mark_job_failed(job, connection, None, None, None)


def expire_jobs(session: Session, receipt_id: str | None = None) -> None:
    now = datetime.now(timezone.utc)
    expired = (
        ((Receipt.status == "queued") & (Receipt.updated_at < now - timedelta(minutes=15)))
        | ((Receipt.status == "processing") & (Receipt.updated_at < now - timedelta(minutes=3)))
    )
    statement = update(Receipt).where(Receipt.user_id == owner, expired)
    if receipt_id is not None:
        statement = statement.where(Receipt.id == receipt_id)
    session.execute(statement.values(status="failed", error="Processing stopped. Retry to try again."))
    session.commit()


def get_receipt(session: Session, receipt_id: str) -> Receipt:
    receipt = session.scalar(select(Receipt).where(Receipt.id == receipt_id, Receipt.user_id == owner))
    if receipt is None:
        raise HTTPException(404, "Document not found.")
    return receipt


def view(receipt: Receipt) -> ReceiptView:
    created_at = receipt.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return ReceiptView(
        id=receipt.id, filename=receipt.filename, content_type=receipt.content_type,
        size=receipt.size, status=receipt.status, created_at=created_at,
        page_count=receipt.page_count, error=receipt.error,
        preview_url=f"/api/receipts/{receipt.id}/preview" if receipt.status in ("ready", "failed") and receipt.page_count else None,
    )


def identify(data: bytes) -> str:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(415, "Choose a PDF, JPEG, PNG, or WebP file.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        pending = session.scalars(
            select(Extraction).outerjoin(SavedValidation).where(SavedValidation.extraction_id.is_(None)),
        )
        for extraction in pending:
            session.add(validation_for(extraction.id, ReceiptExtraction.model_validate(extraction.fields)))
        session.commit()
    yield


app = FastAPI(title="AI Expense Scanner", lifespan=lifespan)


@app.get("/api/receipts")
def list_receipts() -> list[ReceiptView]:
    with Session(engine) as session:
        expire_jobs(session)
        rows = session.scalars(select(Receipt).where(Receipt.user_id == owner).order_by(Receipt.created_at.desc()))
        return [view(receipt) for receipt in rows]


@app.post("/api/receipts", status_code=status.HTTP_202_ACCEPTED)
def upload(file: Annotated[UploadFile, File()], response: Response, tasks: BackgroundTasks) -> ReceiptView:
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, "Choose a file up to 20 MB.")
    content_type = identify(data)
    digest = hashlib.sha256(data).hexdigest()
    receipt_id, attempt = uuid4().hex, uuid4().hex
    key = f"originals/{receipt_id}"
    existing_query = select(Receipt).where(Receipt.user_id == owner, Receipt.sha256 == digest)
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        existing = session.scalar(existing_query)
        if existing:
            response.status_code = 200
            return view(existing)
        store(key, data, content_type)
        session.add(Receipt(
            id=receipt_id, user_id=owner, filename=(file.filename or "Untitled document").replace("\\", "/").split("/")[-1][:255],
            content_type=content_type, size=len(data), storage_key=key, sha256=digest,
            status="queued", attempt=attempt, created_at=now, updated_at=now,
        ))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            discard(key)
            existing = session.scalar(existing_query)
            if existing is None:
                raise
            response.status_code = 200
            return view(existing)

    start_job(tasks, receipt_id, attempt)
    return receipt_details(receipt_id)


def start_job(tasks: BackgroundTasks, receipt_id: str, attempt: str) -> None:
    if redis_url:
        schedule(receipt_id, attempt)
    else:
        tasks.add_task(schedule, receipt_id, attempt)


@app.get("/api/receipts/{receipt_id}")
def receipt_details(receipt_id: str) -> ReceiptView:
    with Session(engine) as session:
        expire_jobs(session, receipt_id)
        return view(get_receipt(session, receipt_id))


@app.get("/api/receipts/{receipt_id}/preview")
def receipt_preview(receipt_id: str) -> Response:
    with Session(engine) as session:
        receipt = get_receipt(session, receipt_id)
        if receipt.status not in ("ready", "failed") or receipt.page_count is None:
            raise HTTPException(404, "Preview is not available yet.")
        return Response(
            load(preview_key(receipt.id, receipt.attempt)),
            media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"},
        )


@app.get("/api/receipts/{receipt_id}/original")
def receipt_original(receipt_id: str) -> Response:
    with Session(engine) as session:
        receipt = get_receipt(session, receipt_id)
        return Response(
            load(receipt.storage_key), media_type=receipt.content_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(receipt.filename, safe='')}"},
        )


@app.post("/api/receipts/{receipt_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry(receipt_id: str, tasks: BackgroundTasks) -> ReceiptView:
    return queue_again(receipt_id, tasks, "failed")


@app.post("/api/receipts/{receipt_id}/extract", status_code=status.HTTP_202_ACCEPTED)
def extract_again(receipt_id: str, tasks: BackgroundTasks) -> ReceiptView:
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "Set OPENAI_API_KEY to enable extraction.")
    return queue_again(receipt_id, tasks, "ready")


def queue_again(receipt_id: str, tasks: BackgroundTasks, expected_status: str) -> ReceiptView:
    attempt = uuid4().hex
    with Session(engine) as session:
        expire_jobs(session, receipt_id)
        get_receipt(session, receipt_id)
        changed = session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.user_id == owner, Receipt.status == expected_status)
            .values(status="queued", attempt=attempt, page_count=None, error=None, updated_at=datetime.now(timezone.utc))
        ).rowcount
        if not changed:
            raise HTTPException(409, "This document cannot be queued in its current state.")
        session.commit()
    start_job(tasks, receipt_id, attempt)
    return receipt_details(receipt_id)


@app.get("/api/receipts/{receipt_id}/extraction")
def receipt_extraction(receipt_id: str) -> ExtractionView:
    with Session(engine) as session:
        receipt = get_receipt(session, receipt_id)
        extraction = session.scalar(
            select(Extraction).where(Extraction.receipt_id == receipt_id)
            .order_by(Extraction.created_at.desc(), Extraction.id.desc()).limit(1)
        )
        if extraction is None:
            raise HTTPException(404, "No completed extraction is available.")
        return ExtractionView(
            id=extraction.id, model=extraction.model, prompt_version=extraction.prompt_version,
            latency_ms=extraction.latency_ms, fields=ReceiptExtraction.model_validate(extraction.fields),
            validation=report_with_duplicates(session, session.get(SavedValidation, extraction.id), receipt),
        )
