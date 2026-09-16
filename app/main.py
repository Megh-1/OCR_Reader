from fastapi import FastAPI

from app.modules.ingestion import router as ingestion_router

app = FastAPI(title="IDP Pipeline")

app.include_router(ingestion_router)

# Future modules (2-9) mount their routers here, e.g.:
# app.include_router(preprocessing_router)
