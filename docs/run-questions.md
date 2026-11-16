# Run questions (for the owner; the run did not wait for any of them)

## Labels to check (Step 3, label_suspect)
The draft labels of these DEV fields are flagged uncertain in data/real_awb/labels/ and the reader disagrees with them; a person should read the
page and either confirm the label or fix it (fill verified_by in data/eval_real/dev/labels.csv). The labels were not changed.

- dev doc 01: charges_code, hs_code
- dev doc 02: charges_code, flight_date, hs_code
- dev doc 06: hs_code, total_other_charges_agent
- dev doc 08: consignee_address
- dev doc 10: invoice_reference, nature_of_goods
- dev doc 11: hs_code
- dev doc 12: first_carrier_code, invoice_reference

## Label conventions that decide several misses (dev)
- agent_name: the form box is "Issuing Carrier's Agent Name and City"; the draft labels hold the name only, the reader (correctly per the box) adds the city. Which is wanted?
- nature_of_goods: the drafts keep only the first line of a multi-line description in some documents; the reader returns the whole cell.
- accounting_information: the drafts keep the printed quote marks around the text, the reader drops them.
- flight_date: dev labels carry "flight/day" without a month; the template regex expects a month (DDMMM).

## GPU host
- The account on the lab host has a 200,000-FILE quota that was already full (other folders hold ~110k files), which stopped the vLLM install; Ollama (80 files) fit. Nothing outside ~/idp_sprint was touched.
- Ollama's default `qwen3-vl:8b` tag is the THINKING variant (all tokens spent reasoning, empty answer); the `-instruct` tag is what is served. It is a Q4_K_M quantisation (6.2 GB), not bf16.
- Step 6 (own adapter): qwen_waybill as a crop engine was not measured on DEV: its weights are not on the GPU host inside the sprint folder and the 200k-file quota left no room to add its environment; skipped.
