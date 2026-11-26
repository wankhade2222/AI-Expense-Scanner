import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine

from backend import main


class UploadTests(unittest.TestCase):
    def setUp(self):
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
