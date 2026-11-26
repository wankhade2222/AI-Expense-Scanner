import io
import os
import unittest
from json import dumps
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine

from backend import main
from backend.extraction_samples import SAMPLES, render_sample


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        directory = self.enterContext(TemporaryDirectory())
        database = create_engine(f"sqlite:///{(Path(directory) / 'test.db').as_posix()}")
        self.addCleanup(database.dispose)
        self.enterContext(patch.multiple(
            main, data_dir=Path(directory), engine=database, bucket=None, redis_url=None,
        ))
        self.client = self.enterContext(TestClient(main.app))

    def upload(self, name, data):
        return self.client.post("/api/receipts", files={"file": (name, data)})

    def test_photo_pdf_preview_duplicate_and_original(self):
        image = Image.new("RGB", (40, 70), "white")
        photo, pdf = io.BytesIO(), io.BytesIO()
        image.save(photo, "JPEG")
        image.save(pdf, "PDF", save_all=True, append_images=[image.copy()])

        for name, data, pages in [("receipt.jpg", photo.getvalue(), 1), ("invoice.pdf", pdf.getvalue(), 2)]:
            uploaded = self.upload(name, data)
            self.assertEqual(uploaded.status_code, 202, uploaded.text)
            receipt_id = uploaded.json()["id"]
            detail = self.client.get(f"/api/receipts/{receipt_id}").json()
            self.assertEqual(detail["status"], "ready")
            self.assertEqual(detail["page_count"], pages)
            preview = self.client.get(detail["preview_url"])
            self.assertEqual(preview.headers["content-type"], "image/jpeg")
            with Image.open(io.BytesIO(preview.content)) as result:
                result.verify()
            self.assertEqual(self.client.get(f"/api/receipts/{receipt_id}/original").content, data)
            duplicate = self.upload(f"duplicate-{name}", data)
            self.assertEqual(duplicate.status_code, 200)
            self.assertEqual(duplicate.json()["id"], receipt_id)

        self.assertEqual(len(self.client.get("/api/receipts").json()), 2)

    def test_transient_failure_can_be_retried_from_original(self):
        photo = io.BytesIO()
        Image.new("RGB", (40, 70)).save(photo, "PNG")
        with patch.object(main, "prepare_preview", side_effect=OSError("Temporary failure")):
            receipt_id = self.upload("receipt.png", photo.getvalue()).json()["id"]
        path = f"/api/receipts/{receipt_id}"
        self.assertEqual(self.client.get(path).json()["status"], "failed")
        self.assertEqual(self.client.get(f"{path}/preview").status_code, 404)
        self.assertEqual(self.client.post(f"{path}/retry").status_code, 202)
        self.assertEqual(self.client.get(path).json()["status"], "ready")
        self.assertEqual(self.client.post(f"{path}/retry").status_code, 409)

    def test_unsupported_file(self):
        self.assertEqual(self.upload("document.txt", b"plain text").status_code, 415)
        self.assertEqual(self.client.get("/api/receipts").json(), [])

    def test_labelled_samples_through_structured_extraction(self):
        import httpx

        def respond(url, *, json, headers, timeout):
            images = [part for part in json["input"][0]["content"] if part["type"] == "input_image"]
            self.assertEqual(len(images), sample.pages)
            self.assertTrue(json["text"]["format"]["strict"])
            payload = {name: None for name in main.ReceiptExtraction.model_fields}
            payload.update(
                merchant=sample.merchant, document_date=sample.document_date, currency=sample.currency,
                total=sample.total, payment_status=sample.payment_status, due_date=sample.due_date,
                document_type=sample.document_type, line_items=[], sources=[],
            )
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"status": "completed", "model": "fixture-model", "output": [{
                    "type": "message", "content": [{"type": "output_text", "text": dumps(payload)}],
                }]},
            )

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), patch("backend.extraction.httpx.post", respond):
            for sample in SAMPLES:
                with self.subTest(merchant=sample.merchant):
                    content, content_type = render_sample(sample)
                    extension = "pdf" if content_type == "application/pdf" else "jpg"
                    receipt = self.upload(f"{sample.merchant}.{extension}", content)
                    path = f"/api/receipts/{receipt.json()['id']}"
                    self.assertEqual(self.client.get(path).json()["status"], "ready")
                    extraction = self.client.get(f"{path}/extraction").json()
                    self.assertEqual(extraction["model"], "fixture-model")
                    for name in ("merchant", "total", "document_date", "currency", "payment_status", "due_date"):
                        self.assertEqual(extraction["fields"][name], getattr(sample, name))

    def test_invalid_model_output_can_be_retried_without_reupload(self):
        import httpx

        def respond(text):
            return httpx.Response(
                200, request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
                json={"status": "completed", "output": [{
                    "type": "message", "content": [{"type": "output_text", "text": text}],
                }]},
            )

        fields = {name: None for name in main.ReceiptExtraction.model_fields}
        fields.update(payment_status="unknown", sources=[], line_items=[])
        responses = [respond("{}"), respond(dumps(fields)), respond(dumps(fields))]
        content, _ = render_sample(SAMPLES[0])
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), patch(
            "backend.extraction.httpx.post", side_effect=responses,
        ):
            receipt = self.upload("retry.jpg", content)
            path = f"/api/receipts/{receipt.json()['id']}"
            failed = self.client.get(path).json()
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(self.client.get(failed["preview_url"]).status_code, 200)
            self.assertEqual(self.client.get(f"{path}/extraction").status_code, 404)
            self.assertEqual(self.client.post(f"{path}/retry").status_code, 202)
            first = self.client.get(f"{path}/extraction").json()
            self.assertIsNone(first["fields"]["total"])
            self.assertEqual(self.client.post(f"{path}/extract").status_code, 202)
            latest = self.client.get(f"{path}/extraction").json()
            self.assertNotEqual(first["id"], latest["id"])
            with main.Session(main.engine) as session:
                self.assertIsNotNone(session.get(main.Extraction, first["id"]))
