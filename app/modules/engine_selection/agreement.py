"""
Two engines read the same page; what do their answers say about each value?

  the same non-empty value  both models read it independently. With mode `trust` (the pair was MEASURED to be wrong rarely when they
                            agree, selector._agreement) the value counts as gated - the same standing a value gets when the word
                            reader agrees or an arithmetic fact holds, i.e. a spot check at VLM_GATED_CONFIDENCE, still never auto.
                            With mode `caution` it keeps the primary engine's own standing: agreement of unmeasured engines proves
                            nothing (Tesseract agreed with the model on 55 of 624 uncertain values and was right on 1 of 117 of its
                            errors, so agreement between engines is evidence only once it has been measured).
  a different value         DISAGREEMENT. The value stays the primary engine's (so the reviewer sees what it read) but its confidence
                            is 0, which Module 7 rejects: the field goes to review. This only ever adds caution, in every mode.
  both blank                nothing to say; a blank is already handled by the field's own rules.
"""

from __future__ import annotations

from dataclasses import replace

from app.modules.vlm_reader.decide import Decision, norm


def same_value(a: str, b: str) -> bool:
    return norm(a).lower() == norm(b).lower()


def combine(primary: dict[str, Decision], secondary: dict[str, Decision], trust: bool) -> tuple[dict[str, Decision], frozenset]:
    """The decisions with the second reader's answers taken into account, and the set of field ids the two disagreed on."""
    out: dict[str, Decision] = {}
    disagreed = set()
    for fid, d in primary.items():
        other = secondary.get(fid)
        if other is None or (not d.value.strip() and not other.value.strip()):
            out[fid] = d
        elif same_value(d.value, other.value):
            out[fid] = replace(d, gated=True) if trust and d.valid else d
        else:
            disagreed.add(fid)
            out[fid] = d
    return out, frozenset(disagreed)
