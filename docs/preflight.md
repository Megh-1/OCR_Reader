# Preflight (Step 0), 2026-11-15

- 0.1 GPU link: FIXED. `ssh gpu` did not resolve (no ~/.ssh/config); the direct login with the key worked, so a `Host gpu` entry was added to ~/.ssh/config (key auth, BatchMode). Host: Ubuntu 24.04, 2x RTX 5090 (GPU0 17.8 of 32.6 GB used by others, GPU1 1.5 GB used), driver 570.211.01 / CUDA 12.8, ~89 GB free disk (93% full), Python 3.12.3, internet reachable (huggingface.co answers 200), no docker/ollama on PATH.
- 0.2 Repo: OK. Untracked scratch scripts (scripts/_offs2.py, _oracle.py, _vote_exp.py) committed as "WIP before sprint"; local tooling folders (.agents/, .taste-skill/, skills-lock.json) left untracked, not project files. Tag `v0-templates` created; branch `sprint-demo`.
- 0.3 CLAUDE.md: FIXED. The old one was 72 KB; moved to docs/history/CLAUDE-2026-11.md, new short CLAUDE.md written verbatim.
- 0.4 Data: OK. 16 real waybills + labels in data/real_awb (folders Easy / Medium / Difficult, labels/), adapter data/models/wb_v4_epoch3 present (own engine available).
- 0.5 Environment: OK. venv imports `app`; Tesseract 5.5.0 (Windows); laptop C: 44 GB free. WSL keep-alive running; postgres:17 and redis:7-alpine present; stack up as compose project `idps` (override docker-compose.sprint.yml: current source bind-mounted, host.docker.internal, engine list from config/reader_engines.json); API :8010, UI :8080; all containers healthy; templates seeded (bits_goa_id_card, air_waybill, air_waybill_routing, air_waybill_bkk).
- 0.6 Sleep: FIXED. standby-timeout-ac was 300 min, now 0. Restore at FINISH: `powercfg /change standby-timeout-ac 300`.
- 0.7 Test suite: OK. 650 passed, 0 failed, 0 skipped (301 s).
