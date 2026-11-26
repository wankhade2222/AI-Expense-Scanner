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
from sqlalchemy import DateTime, String, UniqueConstraint, create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

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

    try:
        preview, count = prepare_preview(load(storage_key), content_type)
        store(preview_key(receipt_id, attempt), preview, "image/jpeg")
        result = {"status": "ready", "page_count": count, "error": None}
    except Exception as exc:
        logger.exception("Could not prepare receipt %s", receipt_id)
        error = str(exc) if type(exc) is ValueError else "Could not read this file. Retry or upload a fresh copy."
        result = {"status": "failed", "error": error}

    with Session(engine) as session:
        session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.attempt == attempt, Receipt.status == "processing")
            .values(**result, updated_at=datetime.now(timezone.utc))
        )
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
        preview_url=f"/api/receipts/{receipt.id}/preview" if receipt.status == "ready" else None,
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
        if receipt.status != "ready":
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
    attempt = uuid4().hex
    with Session(engine) as session:
        expire_jobs(session, receipt_id)
        get_receipt(session, receipt_id)
        changed = session.execute(
            update(Receipt)
            .where(Receipt.id == receipt_id, Receipt.user_id == owner, Receipt.status == "failed")
            .values(status="queued", attempt=attempt, error=None, updated_at=datetime.now(timezone.utc))
        ).rowcount
        if not changed:
            raise HTTPException(409, "Only failed documents can be retried.")
        session.commit()
    start_job(tasks, receipt_id, attempt)
    return receipt_details(receipt_id)
