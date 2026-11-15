import io, json, sys
from collections import Counter
sys.path.insert(0, "/app"); sys.path.insert(0, "/app/scripts")
import cv2, numpy as np, pytesseract
from PIL import Image
from app import runtime
runtime.configure_tesseract()
from app.modules import pipeline, field_extraction as fe
from app.modules.preprocessing import preprocess_image
from app.modules.type_identification import identify_document_type
from app.modules.template_store import get_template_version
from app.modules.validation import validate_fields
from app.config import PSM_BY_DATA_TYPE, DEFAULT_PSM
from export_batch_json import same
D = "/app/data/demo_50/"; truth = json.load(open(D + "ground_truth.json")); kinds = json.load(open(D + "manifest.json"))["kinds"]
norm = lambda s: "".join(str(s).split()).lower()
def variants(crop, psm):
    g = np.array(crop.convert("L"))
    _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    otsu = cv2.copyMakeBorder(otsu, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    h = g.shape[0]; f = max(1.0, 48.0 / max(h, 1)); f = min(f, 3.0)
    sc = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC) if f > 1.05 else g
    sc = cv2.copyMakeBorder(sc, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=int(np.median(g)))
    out = {}
    for name, im in (("otsu", otsu), ("scale", sc)):
        out[name] = fe._read_at_psm(Image.fromarray(im), psm)[0]
    return out
tab = Counter()
for name in sorted(truth):
    if kinds[name] == "digital": continue
    pages, _m, layers = pipeline.prepare_document(open(D + name, "rb").read(), name, None)
    g = preprocess_image(pages[0], deskew=True); ident = identify_document_type(g.image_bytes, None)
    if not ident.confirmed: continue
    t = get_template_version(ident.template_id, ident.version)
    ex = pipeline.extract_page_fields(t, pages[0], g, None, ident.alignment)
    summ = validate_fields(t, ex.fields)
    img = Image.open(io.BytesIO(ex.review_image_bytes)); img.load()
    boxes = {f.id: f for f in fe._fields_on_page(t, ex.alignment)}
    for fid, acc in summ.accepted.items():
        if acc.approval_tier != "auto": continue
        f = boxes[fid]
        if f.from_field or f.extract_pattern or f.squash_spaces: continue
        box = fe.clamped_crop_box(f.bbox, img.width, img.height, 0)
        crop = img.crop(box)
        gg = np.array(crop.convert("L")); _, ot = cv2.threshold(gg, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        w = ot.shape[1]; hh = ot.shape[0]
        def edge(cols): return float((ot[:, cols] > 0).mean())
        e = {"L1": edge(slice(0, 2)), "R1": edge(slice(w - 2, w)), "R3": edge(slice(w - 4, w)), "T": float((ot[0:1, :] > 0).mean()), "B": float((ot[hh - 1:hh, :] > 0).mean())}
        v = {"otsu": "", "scale": ""}
        right = same(fid, acc.value, truth[name]["fields"][fid])
        ao = norm(v["otsu"]) == norm(acc.value); asc = norm(v["scale"]) == norm(acc.value)
        key = ("right" if right else "WRONG", "Rink" if e["R1"] > 0.0 else "R-", "Link" if e["L1"] > 0.0 else "L-")
        tab[key] += 1
        if not right or e["R1"] > 0: print("  ", "RIGHT" if right else "WRONG", fid, repr(acc.value), {k: round(x, 2) for k, x in e.items()})
        if False: print(name, fid, repr(acc.value), "want", repr(truth[name]["fields"][fid]), v)
for k, n in sorted(tab.items()): print(k, n)
