from fastapi import FastAPI

from app.modules.batch import router as batch_router
from app.modules.ingestion import router as ingestion_router
from app.modules.output_compiler import router as output_router
from app.modules.pipeline import router as pipeline_router
from app.modules.review_groups import router as review_groups_router
from app.modules.review_queue import router as review_router
from app.modules.template_store import router as template_router
from app.runtime import configure_logging, configure_tesseract

configure_tesseract()
configure_logging()

app = FastAPI(title="IDP Pipeline")

app.include_router(ingestion_router)
app.include_router(template_router)
app.include_router(output_router)
app.include_router(review_groups_router)  # /review/groups must be registered before anything reads it as /review/{id}
app.include_router(review_router)
app.include_router(pipeline_router)
app.include_router(batch_router)
