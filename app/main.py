from fastapi import FastAPI

from app.modules.ingestion import router as ingestion_router
from app.modules.output_compiler import router as output_router
from app.modules.template_store import router as template_router

app = FastAPI(title="IDP Pipeline")

app.include_router(ingestion_router)
app.include_router(template_router)
app.include_router(output_router)

# Future modules (9) mount their routers here, e.g.:
# app.include_router(review_router)
