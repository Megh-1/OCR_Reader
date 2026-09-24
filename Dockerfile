# FastAPI + Tesseract + OpenCV in one image, per the reference doc's deployment
# section. Two build targets share one base:
#
#   docker build .                       -> `runtime`: the API / batch worker, no tests
#   docker build --target test .         -> `test`: adds tests/ and scripts/ and runs pytest
#
# tests/fixtures/ (a personal ID-card photo and a waybill with real third-party
# contact data) is excluded by .dockerignore and must never be baked into an image;
# to run the tests that need it, bind-mount it at run time (docker-compose.fixtures.yml).

FROM python:3.13-slim AS base

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


FROM base AS test

COPY conftest.py alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
# tests/test_risk_mitigations.py uses this tracked sample as "a document that is not the ID card".
COPY frontend/public/sample-document.png ./frontend/public/sample-document.png

# The root conftest.py defaults DATABASE_URL to the *_test database only if nothing
# sets it, so compose passes it explicitly: running this against the app database
# would write test templates into it.
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


# Last stage, so it is what a plain `docker build .` produces.
FROM base AS runtime

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
# The checked-in templates. A fresh database has none, so a stack that has just come up
# cannot classify anything until: docker compose exec app python scripts/seed_templates.py
COPY scripts/seed_templates.py ./scripts/seed_templates.py

EXPOSE 8000

# Migrations run automatically on container start, not as a separate
# manual step - if that's ever undesirable (e.g. multiple app replicas
# racing to migrate), split this into an init container/one-shot job instead.
# The batch worker overrides this command (see docker-compose.yml).
CMD ["sh", "-c", "python -m alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
