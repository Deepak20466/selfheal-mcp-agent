# Diagnosis recorded with the AI-generated patch (Claude, in-session)

Provenance: written by Claude (the model running this Claude Code session). Not blind: the
model had earlier seen the operator scripted fixture in this repository. Not run through the
evaluator's provider loop. No provider, API or live call was made for it.

Evidence used: the failing frozen test output (`Decimal('125.00') != Decimal('175')`), the frozen
test body, `create_fee`, `FeeUpdate`, and `update_fee` in `backend/app/routers/fees.py`.

Symptom: creating a fee with amount 100 and product_amount 25 gives balance 125. A PUT changing
only `amount` to 150 leaves balance at 125; the test expects 175.

Root cause: `update_fee` copies updated fields onto the row with `setattr`, and recalculates the
balance only when `balance_amount` or `status` is in the payload. Changing `amount` or
`product_amount` therefore leaves `balance_amount` stale.

Fix design: when `amount` or `product_amount` changes, the caller did not set `balance_amount`
explicitly, and the fee is not PAID, move the balance by the change in total
(`new_total - previous_total`), never below zero. This keeps any partial payment already
recorded, unlike resetting the balance to the full total. Explicit `balance_amount` edits and
PAID fees keep their existing behaviour. Only `backend/app/routers/fees.py` is changed.
