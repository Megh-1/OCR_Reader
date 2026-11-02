"""
Builds a reader profile (app/modules/vlm_reader/profile.py) for one waybill form from the labelled corpus.

  IDP_FORM=B python scripts/build_reader_profile.py            # data/reader_profiles/waybill_B.json (Bangkok-printer form)
  IDP_FORM=M python scripts/build_reader_profile.py            # data/reader_profiles/waybill_M.json (Mumbai-printer form)

The profile holds the form's printed rules, the reference page's words outside every value box and the form's own printed text as
boxes. It is made from a real document, so it can carry whatever that page printed outside its value boxes: the output folder is
git-ignored and the file is not to be committed. Point the app at the folder with VLM_READER_PROFILE_DIR.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import make_crops as C  # noqa: E402
from scripts import proto_select as P  # noqa: E402


def build() -> dict:
    C.load_reference_printed()
    return {
        "form": P.FORM,
        "reference": C.REFERENCE,
        "rules": {"h": [list(r) for r in C.RULES["h"]], "v": [list(r) for r in C.RULES["v"]]},
        "printed": [list(t) for t in C.REF_PRINTED],
        "form_blobs": [list(b) for b in C.form_blobs()],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(P.ROOT.parent / "reader_profiles" / f"waybill_{P.FORM}.json"))
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = build()
    out.write_text(json.dumps(profile), encoding="utf-8")
    print(f"{out}: {len(profile['rules']['h'])} horizontal and {len(profile['rules']['v'])} vertical rules, "
          f"{len(profile['printed'])} printed words, {len(profile['form_blobs'])} form blocks")


if __name__ == "__main__":
    main()
