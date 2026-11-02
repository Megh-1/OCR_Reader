"""
LoRA fine-tune of a vision-language model on (crop, question, answer) triples.  Runs ON THE GPU BOX.

    CUDA_VISIBLE_DEVICES=1 python finetune_lora.py --train trainset/syn_train.jsonl trainset/real_train.jsonl --repeat real_train=3 \
        --out adapters/bkk_v2 --epochs 2
    CUDA_VISIBLE_DEVICES=0,1 python -m torch.distributed.run --nproc_per_node=2 finetune_lora.py ... --mem-fraction 0.58,0.62   # both cards

With two cards each process reads half of every batch and their gradients are summed before the step (no DDP wrapper: a sample that runs
out of memory on a shared card is just skipped, which a wrapper's hooks would not survive).

Only the language model's linear layers get adapters (r=16); the vision tower and the 7B base weights are frozen, so the result is a
few tens of MB that can be loaded on top of the untouched model - or left off. The loss is computed on the ANSWER tokens only: the
model learns what to say about a crop, not to reproduce the question. Held-out documents are never given to this script.
"""

import argparse
import gc
import json
import math
import os
import random
import time
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

from prompts import open_crop, prepare, prepared_size

BASE = Path.home() / "idp_work"


def encode(processor, sample: dict):
    image = prepare(open_crop(sample["_root"], sample["image"]))
    user = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": sample["prompt"]}]}]
    prompt_text = processor.apply_chat_template(user, tokenize=False, add_generation_prompt=True)
    full_text = prompt_text + sample["answer"] + "<|im_end|>\n"
    full = processor(text=[full_text], images=[image], return_tensors="pt")
    n_prompt = processor(text=[prompt_text], images=[image], return_tensors="pt")["input_ids"].shape[1]
    labels = full["input_ids"].clone()
    labels[:, :n_prompt] = -100  # no loss on the question
    return full, labels


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-VL-7B-Instruct")
    ap.add_argument("--train", nargs="+", required=True, help="jsonl files, relative to ~/idp_work; each one's crops are in a crops/ folder beside it")
    ap.add_argument("--repeat", nargs="*", default=[], help="name=k : use that file k times per epoch (small real sets)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--accum", type=int, default=8)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--mem-fraction", default="0.8", help="one per card when several, comma separated")
    ap.add_argument("--max-samples", type=int, default=0, help="for a quick speed test")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rank, world, device = int(os.environ.get("RANK", 0)), int(os.environ.get("WORLD_SIZE", 1)), int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(device)
    if world > 1:
        torch.distributed.init_process_group(backend="nccl")
    random.seed(args.seed)  # the same on every card: they shuffle alike and take turns through the list
    torch.manual_seed(args.seed)
    fractions = [float(x) for x in args.mem_fraction.split(",")]
    torch.cuda.set_per_process_memory_fraction(fractions[min(device, len(fractions) - 1)], device)
    repeat = {k: int(v) for k, v in (r.split("=") for r in args.repeat)}
    samples = []
    for name in args.train:
        path = BASE / name
        rows = [{**json.loads(l), "_root": str(path.parent)} for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        samples += rows * repeat.get(path.stem, 1)
    if args.max_samples:
        samples = random.sample(samples, min(args.max_samples, len(samples)))
    for s in samples:
        s["_area"] = (lambda wh: wh[0] * wh[1])(prepared_size(*open_crop(s["_root"], s["image"]).size))
    if rank == 0:
        print(f"{len(samples)} samples per epoch on {world} card(s)", flush=True)

    processor = AutoProcessor.from_pretrained(args.model)
    model = Qwen2_5_VLForConditionalGeneration.from_pretrained(args.model, dtype=torch.bfloat16, device_map={"": device})
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()
    lora = LoraConfig(r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05, task_type="CAUSAL_LM",
                      target_modules=r".*language_model.*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)")
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()
    params = [p for p in model.parameters() if p.requires_grad]
    if world > 1:
        for p in params:  # the cards must start from the same adapter
            torch.distributed.broadcast(p.data, src=0)
    torch.manual_seed(args.seed + rank)  # and drop out differently
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.0)
    total_steps = math.ceil(len(samples) * args.epochs / args.accum)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / 10) * 0.5 * (1 + math.cos(math.pi * min(1.0, s / max(1, total_steps)))))

    model.train()
    t0 = time.perf_counter()
    step = micro = skipped = 0
    running = []
    local_accum = max(1, args.accum // world)
    for epoch in range(args.epochs):
        random.shuffle(samples)
        # Every card takes the same number of samples, or the summing would hang. The cards are shared and the last one has the most room
        # (measured with 18.9 GB against 20.2 GB: 27% of samples ran out of memory against none, always the large crops), so of each
        # group the larger crop goes to the later card.
        mine = [sorted(samples[i:i + world], key=lambda s: s["_area"])[rank] for i in range(0, len(samples) - world + 1, world)]
        for sample in mine:
            out_of_memory = False
            try:
                enc, labels = encode(processor, sample)
                enc = {k: v.to(f"cuda:{device}") for k, v in enc.items()}
                loss = model(**enc, labels=labels.to(f"cuda:{device}")).loss / args.accum
                loss.backward()
                running.append(loss.item() * args.accum)
            except torch.cuda.OutOfMemoryError:  # other people's jobs share these cards: a sample that does not fit is skipped, not fatal
                out_of_memory = True
            if out_of_memory:
                skipped += 1
                enc = labels = loss = None
                gc.collect()
                torch.cuda.empty_cache()
            micro += 1
            if micro % local_accum == 0:
                if world > 1:
                    flat = torch.cat([(p.grad if p.grad is not None else torch.zeros_like(p)).flatten().float() for p in params])
                    torch.distributed.all_reduce(flat)
                    at = 0
                    for p in params:
                        p.grad = flat[at:at + p.numel()].view_as(p).to(p.dtype)
                        at += p.numel()
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step(); sched.step(); opt.zero_grad(set_to_none=True)
                step += 1
                if step % 10 == 0 and rank == 0:
                    print(f"epoch {epoch + 1} step {step}/{total_steps}  loss {sum(running[-80:]) / max(1, len(running[-80:])):.4f}  skipped {skipped}  {time.perf_counter() - t0:.0f}s", flush=True)
        if rank == 0:
            out = BASE / args.out / f"epoch{epoch + 1}"
            model.save_pretrained(out)
            print(f"saved {out}", flush=True)
        if world > 1:
            torch.distributed.barrier()
    print(f"done in {time.perf_counter() - t0:.0f}s  (card {device} skipped {skipped} samples)", flush=True)
    if world > 1:
        torch.distributed.destroy_process_group()


if __name__ == "__main__":
    main()
