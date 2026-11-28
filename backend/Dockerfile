FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/main.py backend/auth.py backend/transfer_legacy.py backend/extraction.py backend/extraction_samples.py backend/evaluation.py backend/validation.py backend/tracker.py backend/
