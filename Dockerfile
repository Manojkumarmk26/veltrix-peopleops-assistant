# Veltrix PeopleOps Assistant — production image (CPU only)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# Tesseract for scanned PDF pages; curl for the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng curl \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch first (avoids the multi-GB CUDA wheels), then the rest.
COPY requirements.txt .
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1 \
    && pip install -r requirements.txt

# Bake the embedding model and the cl100k_base encoding into the image so
# start-up needs no network access and stays well inside the 10-minute limit.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2', device='cpu'); import tiktoken; tiktoken.get_encoding('cl100k_base')"

COPY app/ ./app/

EXPOSE 8000

# /health returns 503 while ingestion runs and 200 {"status":"ok"} when ready.
HEALTHCHECK --interval=10s --timeout=5s --start-period=300s --retries=30 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker: sessions and pending confirmations are held in process memory.
# Runs as root because ./data is bind-mounted from the host and the SQLite
# database (and index) must be writable regardless of host file ownership.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
