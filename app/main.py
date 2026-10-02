"""FastAPI 진입점. Step 3에서 API가 붙는다."""

from fastapi import FastAPI

from app import config

config.ensure_dirs()
app = FastAPI(title="까치녹음기")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "data_dir": str(config.DATA_DIR)}
