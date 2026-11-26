# AI Expense Scanner

## M4 review and confirmation

Select **Review** on a Ready document with a completed extraction. Flagged fields appear first. Select **Edit** to change a field and see a source crop or page when available. Each saved change creates an audit entry and reruns the validation rules. Resolve required flags, then select **Confirm document**. The tracker record keeps the reviewed values and version.

The API exposes `GET /api/receipts/{id}/review`, `PATCH /api/receipts/{id}/fields/{field}`, and `POST /api/receipts/{id}/approve`. Edit and approval requests include the extraction ID and review revision. If another edit changes that version, reload the review. Resubmitting a confirmed version returns the same result.

## M3 validation and flags

Open a processed document preview to see its review flags. The extraction endpoint also returns a `validation` object with amounts in integer minor units, reconciliation status, flag severity, a reason, and source page or region when available. Reconciliation checks tax-inclusive and tax-exclusive interpretations and allows one minor unit of rounding. Possible duplicate checks are scoped to the local user and compare different uploads, with different explicit supplier invoice numbers treated as separate.

Run the validation and API tests from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest backend.test_validation backend.test_uploads
```

## M2 structured extraction

Set `OPENAI_API_KEY` in the API process environment for local Python, or place it in the ignored `.env` file for Docker Compose. Restart the API and worker after changing configuration. `OPENAI_MODEL` defaults to `gpt-4.1-mini`. Without a key, uploads still produce previews.

With extraction enabled, all pages of each new document go to OpenAI. Use synthetic or redacted samples. The API stores typed fields and the raw response with the model and prompt version. Read the most recent successful result at `GET /api/receipts/{id}/extraction`. Use `POST /api/receipts/{id}/extract` on an existing Ready document to run extraction or create a new version.

To compare live model output against 20 labelled synthetic receipts and invoices, set the key in your shell and run `./.venv/Scripts/python.exe -m backend.extraction_samples`. This makes 20 billable API requests on a complete run. The regular unit tests use mocked provider responses.

A receipt and supplier invoice scanner for solo freelancers. M1 covers upload, private file storage, previews, status, duplicate detection, and retry.

## Local development

Install Python 3.12 or 3.13 and Node.js 22.12 or newer. Run the following from the repository root in PowerShell.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload
```

In a second terminal:

```powershell
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run dev
```

Open the Local URL Vite prints. By default the API uses SQLite, local files under `.data/`, and in-process background preview jobs. M1 uses a single local identity. Keep the API and UI on loopback until real authentication is implemented.

## PostgreSQL and Redis worker

With Docker running, use `docker compose up --build` in place of the Python server command. This starts PostgreSQL, Redis, the API, and the RQ worker. Start Vite separately using the same frontend commands.

Compose defaults to a shared file volume. To use a private AWS S3 bucket, copy `.env.example` to `.env`, fill in the bucket and AWS region, and provide AWS credentials through the shell or your local secret workflow. The API and worker both need object access. Use synthetic documents in development.

## Verify M1

```powershell
.\.venv\Scripts\python.exe -m unittest backend.test_uploads
npm.cmd --prefix frontend run build
```

Upload a photo and a multi-page PDF. Check that they become Ready, open their thumbnails, and confirm that the PDF displays its page count and lets you download the original. Upload the exact same PDF again and verify the list still has one copy of it. For a failure-path check, upload a file with a PDF header and invalid contents. It should show Failed and offer Retry from the stored original.
