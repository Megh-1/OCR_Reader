"""
Choosing which reader engines read a template, and in what order, from measurements (docs/engine-selection.md).

    probe.py      what a page looks like (text layer, sharpness, script)                       -> DocFeatures
    selector.py   select_chain(template_id, features) -> ChainDecision   (pure rules + a DB-backed wrapper)
    promotion.py  may a measured engine join a template's chain? (it must beat the incumbent on the same documents)
    drift.py      has an engine got worse in live use than its scorecard said?
    agreement.py  two engines' answers for one page, combined (disagreement -> review)
    store.py      scorecards, pair statistics, promotions, the chain log and human labels

Nothing here makes an engine more trusted than evidence allows: a template with no promoted engine is read in cold-start mode, in
which nothing reaches `auto`.
"""
