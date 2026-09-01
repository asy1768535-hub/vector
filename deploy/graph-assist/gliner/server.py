from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import torch
from fastapi import FastAPI, HTTPException
from gliner import GLiNER
from pydantic import BaseModel, Field


MODEL_ID = os.environ.get("GLINER_MODEL", "urchade/gliner_multi-v2.1")
MODEL_REVISION = os.environ.get(
    "GLINER_REVISION", "443d26d654e0324125a96bebd8e796c14ff2efe6"
)
MAX_LENGTH = int(os.environ.get("GLINER_MAX_LENGTH", "1024"))
MAX_BATCH_SIZE = int(os.environ.get("GLINER_MAX_BATCH_SIZE", "32"))

model: GLiNER | None = None


class ExtractionRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_SIZE)
    labels: list[str] = Field(min_length=1, max_length=64)
    threshold: float = Field(default=0.45, ge=0.0, le=1.0)


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        return value.item()
    return value


@asynccontextmanager
async def lifespan(_: FastAPI):
    global model
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for the GLiNER service")
    model = GLiNER.from_pretrained(
        MODEL_ID,
        revision=MODEL_REVISION,
        max_length=MAX_LENGTH,
        dtype="fp16",
        low_cpu_mem_usage=True,
    ).to("cuda")
    model.eval()
    yield
    model = None
    torch.cuda.empty_cache()


app = FastAPI(title="GLiNER graph assist", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": model is not None,
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "max_length": MAX_LENGTH,
        "max_batch_size": MAX_BATCH_SIZE,
    }


@app.post("/extract")
def extract(request: ExtractionRequest) -> dict[str, Any]:
    if model is None:
        raise HTTPException(status_code=503, detail="model_not_ready")
    if any(not text.strip() or len(text) > 20_000 for text in request.texts):
        raise HTTPException(status_code=422, detail="invalid_text_length")
    if any(not label.strip() or len(label) > 100 for label in request.labels):
        raise HTTPException(status_code=422, detail="invalid_label")

    with torch.inference_mode():
        entities = model.batch_predict_entities(
            request.texts,
            request.labels,
            threshold=request.threshold,
            batch_size=min(MAX_BATCH_SIZE, len(request.texts)),
        )
    return {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "results": _json_value(entities),
    }
