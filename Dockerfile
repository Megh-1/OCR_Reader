# FastAPI + Tesseract + OpenCV in one image, per the reference doc's
# deployment section. NOT built or run as part of this change - Docker
# isn't installed on the machine this was written on; verify with
# `docker-compose up` on a machine that has it before relying on this.

FROM python:3.13-slim

# tesseract-ocr: the actual OCR binary pytesseract shells out to (Modules
#   2's OSD fallback, 5's anchor check, 6's field extraction).
# libmagic1: the real MIME-sniffing library python-magic binds to on Linux
#   (Module 1) - requirements.txt's platform marker already selects the
#   correct python-magic variant per OS, this is that variant's system dep.
# libgl1 / libglib2.0-0: runtime shared libraries opencv-python-headless
#   still needs even in its "headless" build, on Debian.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    libmagic1 \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

EXPOSE 8000

# Migrations run automatically on container start, not as a separate
# manual step - if that's ever undesirable (e.g. multiple app replicas
# racing to migrate), split this into an init container/one-shot job instead.
CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
