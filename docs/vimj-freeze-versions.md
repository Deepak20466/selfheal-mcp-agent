# VIMJ freeze versions

This page explains the two freeze versions for the frozen VIMJ fee-balance case. It does not report a benchmark result.

## Summary

| | V1 | V2 |
|---|---|---|
| Evaluation / challenge ID | `vimj-offline-candidate-2026-11-09` | `vimj-offline-candidate-2026-11-17` |
| Status | Historical, unchanged, **default** | Authoritative, **must be selected explicitly** |
| Model-visible freeze pin | `9c9f1e6e…` (**unreproducible**) | `921fe894…` (matches the committed bytes at commit `066bcf8`) |
| Freeze gate on the checked-in file | **Fails** (file hashes to `921fe894…`) | **Passes** |
| Selector | `--freeze-version v1` (or omitted) | `--freeze-version v2` |

V1 is preserved exactly: its files, hashes, identities, and historical evidence are unchanged. The unreproducible pin is recorded, not fixed. V1 still refuses to run its freeze gate without the diagnostic override.

## Selecting a version

The evaluator and the scripted harness both accept the flag. The default is V1.

```bash
# Historical default (V1); offline baseline stops at the freeze gate today.
python -m benchmarks.real_bugs.unresolved_eval --candidate-root <clean-checkout> --evidence <new>.jsonl

# Explicit V2.
python -m benchmarks.real_bugs.unresolved_eval --candidate-root <clean-checkout> --evidence <new>.jsonl --freeze-version v2
```

Unknown versions fail closed. The argument parser rejects anything other than `v1` or `v2`.

## V2 hash chain

Each file pins the file above it:

```
vimj-fee-balance-v2-model-visible-freeze.json   sha256 921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc
  └─ vimj-fee-balance-v2.json (challenge)        sha256 b67eebaea953c1393d82961c6b8376456ed149c34852b7e35ba8ce4b000ca24f
       └─ vimj-gemini-2.5-flash-budget-v2.json   sha256 786952c696095481fddc40b08a1d47c80f939e1ebf4051f2ecc65895fad62818
            └─ vimj-gemini-2.5-flash-context-v2.json  sha256 7eaa23bcc4a3e7540cb89573b08e262dfc61977d2cbfc72ddbe33c3c60c5eb66
```

The V2 freeze file is a byte-identical copy of the file committed at `066bcf8` (2026-11-09, "Align unresolved evaluation with operator freeze"). Its inner `evaluation_id` still names the 2026-11-09 challenge, because changing those bytes would change the pinned hash. The V2 challenge config records this source explicitly (`model_visible_freeze_source_evaluation_id`, `model_visible_freeze_source_commit`).

The V2 challenge config reuses the V1 regression overlay, `vimj-fee-balance-v1-test.patch`. The overlay file was not changed, and its hash `c77b210c…` is pinned by both versions.

## Shared facts (both versions)

- Baseline commit: `6490833d178b71624d024b16046240a20a3f78a6`
- Source file: `backend/app/routers/fees.py`, sha256 `6d131b6b91d079f61af2d78b1e80e54ab9512093951b8caae0ff5a4735d09d28`
- Regression test: `tests/test_requested_changes.py::RequestedChanges::test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync`, canonical sha256 `c5dd6217…`, raw CRLF sha256 `1bd8dcbe…`
- Caps: 32,768 tokens, 10 requests, $0 cost cap, 600 seconds

## What is not claimed

- The expected baseline failure (`125.00` vs `175`) was **not re-executed** with V2. The Bubblewrap sandbox is blocked by the per-user process cap (`MAX_PROCESSES = 64`). That failure is supported by the saved 2026-11-09 preflight records only.
- No model-generated repair exists for this case in either version.
- Nothing here makes a provider or live model call.

## Where to look

- Decision record and review: `benchmarks/real_bugs/evidence/vimj-freeze-review-2026-11-17.md`
- Freeze records: `benchmarks/real_bugs/unresolved/freeze-records/`
- Implementation report and proof that V1 is unchanged: `benchmarks/real_bugs/evidence/vimj-freeze-v2-implementation-2026-11-17.md`
