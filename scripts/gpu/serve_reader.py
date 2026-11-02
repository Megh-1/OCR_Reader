"""
The reader service: Qwen2.5-VL-7B-Instruct + a LoRA adapter behind one HTTP endpoint. Runs ON A GPU MACHINE, not where the app runs.
The app (app/modules/vlm_reader/client.py) sends field crops and questions; this returns the model's replies.

    pip install torch transformers peft accelerate pillow fastapi "uvicorn[standard]"        # on the GPU machine
    READER_TOKEN=<a long random string> python serve_reader.py --adapter data/models/wb_v4_epoch3 --port 8700 --mem-fraction 0.8

  POST /read    {"max_new": 512, "requests": [{"image_b64": "<PNG, base64>", "prompt": "..."}]}  ->  {"replies": ["...", ...]}
  GET  /health  {"ok": true, "adapter": ..., "adapter_sha256": ...}

Same loading, preparation and greedy decoding as vlm_read.py, the script every measured number came from: the same crop gives the same
reply. One request at a time on the card (a lock), and a request is read in sub-batches of --batch crops sorted by size.

SECURITY: the crops are pieces of business documents. Set READER_TOKEN (the app sends it as a bearer token, VLM_READER_TOKEN), keep the
port off the public internet, and put TLS in front of it (a reverse proxy, or a tunnel) when it is not on a private network.
Not exercised end to end in the session that wrote it (no GPU was available afterwards); the model calls are vlm_read.py's.
"""

import argparse
import base64
import hashlib
import io
import os
import threading
from pathlib import Path

import torch
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from PIL import Image
from pydantic import BaseModel
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

from prompts import prepare

STATE: dict = {}
LOCK = threading.Lock()
app = FastAPI(title="idp vision reader")


class ReadItem(BaseModel):
    image_b64: str
    prompt: str


class ReadRequest(BaseModel):
    max_new: int = 512
    requests: list[ReadItem]


def check_token(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("READER_TOKEN", "")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="bad or missing bearer token")


def generate(items: list[ReadItem], max_new: int, batch: int) -> list[str]:
    model, processor = STATE["model"], STATE["processor"]
    processor.tokenizer.padding_side = "left"
    images = [Image.open(io.BytesIO(base64.b64decode(i.image_b64))) for i in items]
    order = sorted(range(len(items)), key=lambda k: images[k].size[0] * images[k].size[1])  # similar sizes together keep padding small
    replies = [""] * len(items)
    for start in range(0, len(order), batch):
        idx = order[start:start + batch]
        prepared = [prepare(images[k]) for k in idx]
        texts = []
        for k in idx:
            messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": items[k].prompt}]}]
            texts.append(processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))
        inputs = processor(text=texts, images=prepared, padding=True, return_tensors="pt").to("cuda")
        with torch.no_grad():
            ids = model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
        for k, reply in zip(idx, processor.batch_decode(ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)):
            replies[k] = reply.strip()
    return replies


@app.post("/read", dependencies=[Depends(check_token)])
def read(body: ReadRequest) -> dict:
    if not body.requests:
        return {"replies": []}
    if len(body.requests) > 200 or not 1 <= body.max_new <= 2048:
        raise HTTPException(status_code=422, detail="at most 200 crops and 1-2048 new tokens per request")
    with LOCK:
        return {"replies": generate(body.requests, body.max_new, STATE["batch"])}


@app.get("/health")
def health() -> dict:
    return {"ok": "model" in STATE, "adapter": STATE.get("adapter"), "adapter_sha256": STATE.get("adapter_sha256")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-VL-7B-Instruct")
    ap.add_argument("--adapter", required=True, help="a LoRA adapter folder (adapter_model.safetensors + adapter_config.json)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--mem-fraction", type=float, default=0.8)
    ap.add_argument("--batch", type=int, default=4, help="crops per forward pass")
    args = ap.parse_args()

    torch.cuda.set_per_process_memory_fraction(args.mem_fraction)
    processor = AutoProcessor.from_pretrained(args.model)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model, dtype=torch.bfloat16, device_map="cuda").eval()
    from peft import PeftModel
    model = PeftModel.from_pretrained(model, args.adapter).eval()
    weights = Path(args.adapter) / "adapter_model.safetensors"
    STATE.update(model=model, processor=processor, batch=args.batch, adapter=str(args.adapter),
                 adapter_sha256=hashlib.sha256(weights.read_bytes()).hexdigest())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
