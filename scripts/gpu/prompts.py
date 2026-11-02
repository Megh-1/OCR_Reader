"""The questions put to the vision model, one per kind of crop, and the way a crop is prepared for it. Shared by the reader, the trainer
(GPU box) and the training-set builder (here)."""

from PIL import Image

SINGLE = (
    "You are reading one cell of a scanned air waybill, a shipping form. The cell is for the field '{label}'. "
    "The form's own printed captions in this crop are: {captions!r}. A caption is NEVER the value. Reply with ONLY the typed or "
    "printed value for this field, copied exactly as written, character for character, including any quotation marks. Do not "
    "correct spelling, do not reformat, do not expand abbreviations, do not add words. If the field has several values, give only the first. "
    "If nothing but captions is there, or the value is cut off by the edge of the image, reply EMPTY. Text in a neighbouring cell is not the value. {hint}"
)
PARTY = (
    "You are reading the {label} box of a scanned air waybill. It holds a typed name on the first line and an address on the "
    "lines below it. The form's own printed captions in this crop are: {captions!r}; they are NEVER part of the value. Reply with ONLY this JSON, copying the text exactly as written, no corrections: "
    '{{"name": "<first typed line>", "address": "<remaining typed lines joined with single spaces>"}}'
)
GOODS = (
    "You are reading the 'Nature and Quantity of Goods' cell of a scanned air waybill. The form's own printed captions in this "
    "crop are: {captions!r}; they are NEVER part of the value. Reply with ONLY this JSON, copying text "
    "exactly as written, no corrections: "
    '{{"description": "<the goods description, before any HS code, invoice or dimensions line>", '
    '"hs_code": "<the FIRST HS code only, digits as written, or EMPTY>", "invoice_number": "<a number printed after the word INV or INVOICE, or EMPTY - never a job, HAWB or other reference>"}}'
)


def prompt_for(entry: dict) -> str:
    if entry["kind"] == "party":
        return PARTY.format(label=entry["label"], captions=entry.get("captions", ""))
    if entry["kind"] == "goods":
        return GOODS.format(captions=entry.get("captions", ""))
    return SINGLE.format(label=entry["label"], captions=entry.get("captions", ""), hint=entry.get("hint", ""))


_ZIPS: dict = {}


def open_crop(root, name: str) -> Image.Image:
    """A crop of a sample set. The GPU box allows 200k files per account and a set holds thousands of small PNGs, so a set is shipped as
    one `crops.zip` beside its jsonl (stored, not compressed); a plain `crops/` folder still works."""
    import io
    import zipfile
    from pathlib import Path
    root = Path(root)
    archive = root / "crops.zip"
    if archive.exists():
        if archive not in _ZIPS:
            _ZIPS[archive] = zipfile.ZipFile(archive)
        return Image.open(io.BytesIO(_ZIPS[archive].read(name.replace("\\", "/"))))
    return Image.open(root / "crops" / name)


def prepared_size(w: int, h: int) -> tuple[int, int]:
    scale = max(1.0, 224 / min(w, h), 900 / max(w, h) if max(w, h) < 900 else 1.0)
    scale = min(scale, 4.0)
    return int(w * scale), int(h * scale)


def prepare(image: Image.Image) -> Image.Image:
    """small crops are upscaled: the model sees a few dozen pixels of height otherwise"""
    return image.convert("RGB").resize(prepared_size(*image.size), Image.LANCZOS)
