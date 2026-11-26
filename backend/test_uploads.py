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
from backend.extraction import GEMINI_MODEL, ExtractionResult
from backend.extraction_samples import SAMPLES, render_sample
from backend.test_validation import invoice


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "EXTRACTION_PROVIDER": "", "GEMINI_API_KEY": "", "GEMINI_MODEL": "", "OPENAI_API_KEY": "",
        }))
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
        self.check_labelled_samples("openai")

    def test_labelled_samples_through_gemini(self):
        self.check_labelled_samples("gemini")

    def check_labelled_samples(self, provider):
        import httpx

        def respond(url, *, json, headers, timeout):
            payload = {name: None for name in main.ReceiptExtraction.model_fields}
            payload.update(
                merchant=sample.merchant, document_date=sample.document_date, currency=sample.currency,
                total=sample.total, payment_status=sample.payment_status, due_date=sample.due_date,
                document_type=sample.document_type, line_items=[], sources=[],
            )
            if provider == "gemini":
                self.assertEqual(url, f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent")
                self.assertEqual(headers, {"x-goog-api-key": "test-only"})
                config = json["generationConfig"]
                self.assertEqual(config["responseMimeType"], "application/json")
                self.assertEqual(config["responseJsonSchema"], main.ReceiptExtraction.model_json_schema())
                parts = json["contents"][0]["parts"]
                images = [part["inlineData"] for part in parts if "inlineData" in part]
                self.assertEqual(len(images), sample.pages)
                self.assertTrue(all(image["mimeType"] == "image/jpeg" and image["data"] for image in images))
                self.assertEqual([part["text"] for part in parts[1::2]], [
                    f"Page {number}" for number in range(1, sample.pages + 1)
                ])
                reply = {"modelVersion": "fixture-model", "candidates": [{
                    "finishReason": "STOP", "content": {"parts": [
                        {"thought": True, "text": "Internal thought"},
                        {"text": dumps(payload)},
                    ]},
                }]}
            else:
                images = [part for part in json["input"][0]["content"] if part["type"] == "input_image"]
                self.assertEqual(len(images), sample.pages)
                self.assertTrue(json["text"]["format"]["strict"])
                reply = {"status": "completed", "model": "fixture-model", "output": [{
                    "type": "message", "content": [{"type": "output_text", "text": dumps(payload)}],
                }]}
            return httpx.Response(200, request=httpx.Request("POST", url), json=reply)

        with patch.dict(os.environ, {f"{provider.upper()}_API_KEY": "test-only"}), patch(
            "backend.extraction.httpx.post", respond,
        ):
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

    def test_gemini_quota_failure_keeps_preview_and_allows_retry(self):
        import httpx

        with patch.dict(os.environ, {"EXTRACTION_PROVIDER": "gemini", "GEMINI_API_KEY": "test-only"}), patch(
            "backend.extraction.httpx.post", side_effect=lambda url, **kwargs: httpx.Response(
                429, request=httpx.Request("POST", url),
            ),
        ) as request:
            upload = self.upload("quota.jpg", render_sample(SAMPLES[0])[0])
            path = f"/api/receipts/{upload.json()['id']}"
            failed = self.client.get(path).json()
            self.assertEqual(failed["status"], "failed")
            self.assertIn("usage limit", failed["error"])
            self.assertEqual(self.client.get(failed["preview_url"]).status_code, 200)
            self.assertEqual(self.client.post(f"{path}/retry").status_code, 202)
            self.assertEqual(request.call_count, 2)

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

    def test_near_duplicates_are_detected_in_both_directions(self):
        photo = io.BytesIO()
        Image.new("RGB", (30, 50)).save(photo, "JPEG")
        documents = []
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            for index, invoice_number in enumerate(("INV-42", "inv-42", "INV-43")):
                fields = invoice(invoice_number=invoice_number, document_date="2025-09-01")
                with patch.object(main, "extract_receipt", return_value=ExtractionResult(fields, {}, "fixture", 2)):
                    uploaded = self.upload(f"invoice-{index}.jpg", photo.getvalue() + bytes([index]))
                documents.append(uploaded.json()["id"])
        for index, document in enumerate(documents):
            response = self.client.get(f"/api/receipts/{document}/extraction")
            self.assertEqual(response.status_code, 200)
            report = response.json()["validation"]
            self.assertEqual(report["totals_check"], "matched")
            self.assertEqual(any(flag["code"] == "possible_duplicate" for flag in report["flags"]), index < 2)

    def test_review_crop_correction_version_and_repeat_confirmation(self):
        document, _ = render_sample(SAMPLES[10])
        fields = invoice(
            document_date="2025-09-01", currency=None, total="1500",
            sources=[{"field": "total", "page": 2, "box": [0.1, 0.1, 0.5, 0.5]}],
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}), patch.object(
            main, "extract_receipt", return_value=ExtractionResult(fields, {}, "fixture", 2),
        ):
            uploaded = self.upload("review.pdf", document)
        path = f"/api/receipts/{uploaded.json()['id']}"
        initial = self.client.get(f"{path}/review").json()
        version = initial["version"]
        self.assertEqual(self.client.post(f"{path}/approve", json=version).status_code, 422)
        crop = self.client.get(f"{path}/review/source", params={"field": "total", "extraction_id": version["extraction_id"]})
        self.assertEqual(crop.status_code, 200)
        with Image.open(io.BytesIO(crop.content)) as image:
            self.assertLess(image.width, 1000)
            image.verify()

        def edit(field, value, edit_version):
            return self.client.patch(f"{path}/fields/{field}", json={**edit_version, "value": value})

        currency = edit("currency", "INR", version).json()
        self.assertEqual(currency["validation"]["totals_check"], "mismatch")
        self.assertEqual(edit("total", "1416", version).status_code, 409)
        fixed = edit("total", "1416", currency["version"]).json()
        self.assertEqual(fixed["validation"]["totals_check"], "matched")
        self.assertEqual(fixed["fields"]["total"], "1416")
        self.assertEqual(self.client.get(f"{path}/extraction").json()["fields"]["total"], "1500")
        self.assertEqual(self.client.post(f"{path}/approve", json=version).status_code, 409)

        # Two requests for the same reviewed version produce one confirmed expense.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            approvals = list(pool.map(lambda _: self.client.post(f"{path}/approve", json=fixed["version"]), range(2)))
        self.assertTrue(all(result.status_code == 200 and result.json()["confirmed"] for result in approvals))
        self.assertEqual(edit("total", "1440", fixed["version"]).status_code, 409)
        with main.Session(main.engine) as session:
            expense = session.get(main.Expense, uploaded.json()["id"])
            self.assertEqual(expense.amounts_minor["total"], 141_600)
            corrections = session.scalars(main.select(main.Correction).order_by(main.Correction.revision)).all()
            self.assertEqual([(row.field, row.previous_value, row.new_value) for row in corrections], [
                ("currency", None, "INR"), ("total", "1500", "1416"),
            ])
