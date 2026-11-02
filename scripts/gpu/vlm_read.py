"""
Runs a vision-language model over the field crops written by scripts/make_crops.py.  Runs ON THE GPU BOX, not here.

    CUDA_VISIBLE_DEVICES=1 python vlm_read.py --adapter adapters/bkk_v2/epoch2 --also-zero-shot \
        --jobs ts_B/syn_val.jsonl:results/v2_B_synval.jsonl  ev_test_B/eval.jsonl:results/v2_B_test.jsonl  pg_test_pages/pages.jsonl:results/v2_pages.jsonl:1500:1

A job is SAMPLES:OUT[:MAX_NEW[:BATCH]] - a sample jsonl (relative to ~/idp_work; its stored prompts are used and its crops sit in a
crops.zip or crops/ beside it) and where to write the replies. The model is loaded ONCE and every job reads with it. With
--also-zero-shot each job is read a second time with the adapter switched off, written to OUT with ".zs" before the suffix, so the
tuned and untuned readers see exactly the same crops.

The model is asked to COPY what is printed in a crop, never to infer: no spelling fixes, no reformatting, EMPTY when there is
nothing. Greedy decoding, so the same crop always gives the same answer. Uses at most --mem-fraction of the card.
(The older --samples/--docs/--out form still works for a single job.)
"""

import argparse
import json
import time
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

BASE = Path.home() / "idp_work"

from prompts import open_crop, prepare, prompt_for  # noqa: E402


def run(model, processor, manifest, load, out: Path, max_new: int, batch: int) -> None:
    out.parent.mkdir(exist_ok=True, parents=True)
    t0 = time.perf_counter()
    processor.tokenizer.padding_side = "left"  # a batch is padded on the left so generation continues from the real end
    areas = [load(e["image"]).size for e in manifest]
    order = sorted(range(len(manifest)), key=lambda i: areas[i][0] * areas[i][1])
    done = 0
    with out.open("w", encoding="utf-8") as fh:
        for start in range(0, len(order), batch):
            chunk = [manifest[i] for i in order[start:start + batch]]  # similar sizes together keeps padding small
            images = [prepare(load(e["image"])) for e in chunk]
            texts = []
            for e in chunk:
                messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": e.get("prompt") or prompt_for(e)}]}]
                texts.append(processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True))
            inputs = processor(text=texts, images=images, padding=True, return_tensors="pt").to("cuda")
            with torch.no_grad():
                ids = model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
            replies = processor.batch_decode(ids[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
            for e, reply in zip(chunk, replies):
                fh.write(json.dumps({**e, "reply": reply.strip()}, ensure_ascii=False) + "\n")
            fh.flush()
            done += len(chunk)
            if (start // batch) % 20 == 0:
                print(f"  {out.name}: {done}/{len(manifest)}  {time.perf_counter() - t0:.0f}s", flush=True)
    print(f"done {out.name}: {len(manifest)} samples in {time.perf_counter() - t0:.0f}s  ({(time.perf_counter() - t0) / max(1, len(manifest)):.2f}s each)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-VL-7B-Instruct")
    ap.add_argument("--jobs", nargs="*", default=[], help="SAMPLES:OUT[:MAX_NEW[:BATCH]] ...")
    ap.add_argument("--also-zero-shot", action="store_true", help="read every job again with the adapter off (needs --adapter)")
    ap.add_argument("--docs", nargs="*", default=[], help="(old form) document numbers to read from crops/manifest.json")
    ap.add_argument("--samples", help="(old form) one sample jsonl")
    ap.add_argument("--adapter", help="a LoRA adapter folder to load on top of the model")
    ap.add_argument("--out", help="(old form) where to write")
    ap.add_argument("--mem-fraction", type=float, default=0.6)
    ap.add_argument("--max-new", type=int, default=512)  # 200 cut a long goods list mid-JSON, and three fields came back blank
    ap.add_argument("--batch", type=int, default=8, help="crops per forward pass")
    args = ap.parse_args()

    torch.cuda.set_per_process_memory_fraction(args.mem_fraction)
    processor = AutoProcessor.from_pretrained(args.model)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model, dtype=torch.bfloat16, device_map="cuda").eval()
    if args.adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, BASE / args.adapter).eval()

    jobs = []
    for j in args.jobs:
        parts = j.split(":")
        jobs.append((parts[0], parts[1], int(parts[2]) if len(parts) > 2 else args.max_new, int(parts[3]) if len(parts) > 3 else args.batch))
    if args.samples:
        jobs.append((args.samples, args.out, args.max_new, args.batch))
    for samples, out, max_new, batch in jobs:
        path = BASE / samples
        manifest = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        load = lambda name, p=path: open_crop(p.parent, name)  # noqa: E731
        run(model, processor, manifest, load, BASE / out, max_new, batch)
        if args.also_zero_shot:
            zs = Path(out)
            with model.disable_adapter():
                run(model, processor, manifest, load, BASE / zs.with_name(zs.stem + ".zs" + zs.suffix), max_new, batch)
    if args.docs:
        manifest = [e for e in json.loads((BASE / "crops" / "manifest.json").read_text()) if e["doc"] in set(args.docs)]
        run(model, processor, manifest, lambda name: Image.open(BASE / "crops" / name), BASE / args.out, args.max_new, args.batch)


if __name__ == "__main__":
    main()
