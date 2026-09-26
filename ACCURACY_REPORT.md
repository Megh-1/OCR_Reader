# Accuracy report — what the console is detecting, and why

Generated against two live batches (40 documents total, the synthetic waybill corpus,
each file rendered/photographed/scanned from one real air waybill) run through the
containerized stack's `POST /documents/batch`. Numbers below are from
`scripts/evaluate_batch.py --sweep` against the API — not estimated.

## The headline: nothing was ever wrongly auto-approved

Across both runs (text-layer PDF, scanned PDF, and camera-photo tiers, 580 field reads
total), **zero wrong values were approved without a person seeing them.** Every field
that was ever misread and still passed its checks landed in `spot_check`, not `auto`:

| Input tier | fields | auto | auto precision | wrong-but-accepted | all caught in spot_check? |
|---|---:|---:|---:|---:|---|
| Text-layer PDF | 29 | 19 (66%) | 19/19 = 100% | 3 | yes |
| Scanned PDF | 261 | 129 (49%) | 129/129 = 100% | 14 | yes |
| Camera photo | 290 | 128 (44%) | 128/128 = 100% | 5 | yes |

That's the system's core promise working: "a wrong value that looks correct is more
dangerous than an honest refusal." It refuses to trust a value on confidence alone.

## Finding 1 — the case you were probably looking at is correct, not broken

The most visible row in Review right now is `flight_date`, shown as `EK0565/27FEB` at
confidence 74.5, marked **format check failed**. At a glance this looks like the system
rejecting the *right* answer. It isn't — the displayed value is `EKO565/27FEB` (a
**letter O**, not a zero) in a monospace font where the two are hard to tell apart on
screen. Byte-for-byte, from the API:

```
EKO565/27FEB   ['E','K','O',...]   confidence 74.5   format_invalid   <- what was actually read
EK0565/27FEB   ['E','K','0',...]   confidence 80.0   (accepted)        <- the correct value, elsewhere
```

This is *the exact misread* the `flight_date` regex (added in Step 1 of this project)
was written to catch. It is working as designed. The confidence number (74.5) is not
"how likely this is correct" — it's Tesseract's own certainty about the characters it
saw, which is a different thing from whether the box was read off the right pixels or
the value is semantically right. That distinction is the single most common source of
"why does this look wrong" confusion in this UI, and is worth saying out loud when you
present it.

## Finding 2 — a real, previously-undiscovered defect: three field boxes overlap the field below them

This one is real, not a false alarm, and it's the most common wrong-but-accepted case
in both runs. `shipper_name`, `consignee_name`, and `agent_name` on `air_waybill`
systematically pick up the first few words of the *address* field printed directly
below them — even on the clean digital PDF, at 100% confidence, with **no OCR
involved at all**:

```
shipper_name    -> "VTS TF AIR SYSTEMS PVT LTD SURVEY"      (should stop at "LTD")
consignee_name  -> "VTS CLIMA LLC 13-C, PLOT NO."            (should stop at "LLC")
agent_name      -> "TEAMGLOBAL LOGISTICS PVT LTD BANGA"      (should stop at "LTD")
```

**Root cause, confirmed from the template and the code, not guessed:**

`app/modules/field_extraction.py`'s text-layer reader (`_overlap_of_word`) decides
independently, per field, whether a word belongs to it — there's nothing stopping the
same word from counting toward two different fields. That's fine *if* field boxes
don't overlap. Two of them do, right now, in `scripts/seed_templates.py`:

```
shipper_name     y: [0.0525, 0.0672]
shipper_address  y: [0.0600, 0.1020]     <- overlaps shipper_name by 0.0072
consignee_name   y: [0.1425, 0.1581]
consignee_address y:[0.1500, 0.2040]     <- overlaps consignee_name by 0.0081
```

Any word whose box straddles that shared band — which is exactly where the address's
first line sits — gets counted into *both* fields. `agent_name`'s box (0.2304–0.2506,
notably taller than the other two name fields) most likely just extends past its own
line into the agent's printed city, with no second field's box to blame.

**Why this doesn't need a panic fix before your talk:** none of these three fields has
a regex or a checksum (they're free-text names), so Module 7 can never auto-approve
them regardless of confidence — the no-validator rule from Step 1 catches every single
one and routes it to `spot_check`. Nothing shipped silently wrong; a person always sees
it. That's the same safety property as Finding 1, just a different mechanism.

**Why I didn't fix the boxes just now:** shrinking `shipper_name`'s bottom edge from
0.0672 to under 0.0600 removes the overlap arithmetically, but this project's own
convention (see `CLAUDE.md`, and the `air_waybill` v8 fix already in the template) is
to profile real ink rows on a render before moving an edge — a blind number change
risks clipping the value's own text instead of fixing the bleed, trading one bug for a
worse one. This needs the same measured pass as the original box fixes, not a guess
five minutes before a demo. I'm flagging it here so it doesn't get lost; happy to do
that measurement pass right after if you want it fixed properly.

## Finding 3 — accuracy by tier is unchanged from Step 1, still one-waybill caveats apply

| Input tier | Module 5 classification | Field accuracy |
|---|---|---|
| Text-layer PDF | 1/1 correct | 26/29 (90%) |
| Scanned PDF | 9/9 correct | 231/261 (89%) |
| Camera photo | 10/10 correct | 250/290 (86%) |

Every misclassified field is one of: the Finding 2 bleed (most common), a genuine OCR
character error on a low-quality/skewed synthetic photo, or a perspective-warp case
where the box shifts and the *front* of a name gets clipped instead (`AIR SYSTEMS PVT
LTD` losing `VTS TF ` — a different, already-documented limitation of the page-warp
coordinate mapping, not new). This is one waybill rendered 20-40 ways, so these
percentages describe how the *rendering conditions* behave, not a general accuracy
claim across real-world waybills — the same caveat that's been true since Step 0.

## What I changed in the UI just now

1. **Removed a ~200ms blank gap when switching between review items.** The detail
   pane used an "exit old, then enter new" animation; on a slower machine or a fast
   click-through this could read as the UI freezing. Enter and exit now overlap.
2. **Stopped tinting every plain "review" row red.** Only genuinely confident-and-wrong
   items (the ones worth being alarmed about) now carry the warning color; a low-
   confidence rejection that's merely unclear no longer looks identical to a dangerous
   one. This was making the whole queue look like "everything is broken" when most of
   it is ordinary, expected review traffic.

Both are verified against the running container, not just eyeballed.

## Bottom line

- **Nothing wrong has ever been auto-approved.** That's the one property this whole
  design exists to guarantee, and it held across both runs.
- **What looks like a wrong score usually isn't** — it's either the system correctly
  catching a misread character (Finding 1) or a value that's genuinely wrong for a
  reason the UI wasn't explaining clearly enough (Finding 2), which is now on record
  with a precise cause instead of a guess.
- **One real defect is now documented with hard evidence**: three name fields on
  `air_waybill` have overlapping boxes with their address fields. It costs correctly-
  caught spot-check volume, not safety. Fixing it properly means re-measuring the box
  edges the way the project already does for this kind of thing — I'd rather do that
  right after your presentation than guess at it now.
