#!/bin/bash
# Stage 1 of the reader evaluation, run ON THE GPU BOX: the SYNTHETIC validation set and the real TRAIN documents - everything that is
# allowed to inform the choice of adapter (epoch 1 or 2). The held-out documents are NOT read here (stage 2, after the choice).
#
# The box's GPUs are shared (other people's notebooks hold ~22 GB on each). This waits until one card has NEED_MB free and then uses
# only that card, capped to what was free, so nobody else's job is squeezed. Safe to leave running.
cd ~/idp_work || exit 1
NEED_MB=${NEED_MB:-19500}
while true; do
  for g in 0 1; do
    used=$(nvidia-smi --id=$g --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
    total=$(nvidia-smi --id=$g --query-gpu=memory.total --format=csv,noheader,nounits | tr -d ' ')
    if [ $((total - used)) -ge "$NEED_MB" ]; then GPU=$g; FREE=$((total - used)); TOTAL=$total; break 2; fi
  done
  sleep 60
done
FRAC=$(python3 -c "print(round(min(0.95, ($NEED_MB - 500) / $TOTAL), 3))")
echo "stage 1 starts on GPU $GPU at $(date): $FREE MB free, memory fraction $FRAC"
export CUDA_VISIBLE_DEVICES=$GPU
PY=venv/bin/python
R="--mem-fraction $FRAC"
$PY vlm_read.py $R --adapter adapters/bkk_v2/epoch2 --also-zero-shot --jobs ts_B/syn_val.jsonl:results/B_synval_e2.jsonl ev_train_B/eval.jsonl:results/B_train_e2.jsonl
$PY vlm_read.py $R --adapter adapters/bkk_v2/epoch1 --jobs ts_B/syn_val.jsonl:results/B_synval_e1.jsonl
$PY vlm_read.py $R --adapter adapters/m_v2/epoch2 --also-zero-shot --jobs ts_M/syn_val.jsonl:results/M_synval_e2.jsonl ev_train_M/eval.jsonl:results/M_train_e2.jsonl
$PY vlm_read.py $R --adapter adapters/m_v2/epoch1 --jobs ts_M/syn_val.jsonl:results/M_synval_e1.jsonl
echo "stage 1 finished at $(date)"
