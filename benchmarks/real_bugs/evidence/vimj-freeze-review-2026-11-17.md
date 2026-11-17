# VIMJ freeze review: proposed, not authoritative (2026-11-17)

**Written for:** the repository owner, to approve or reject one freeze approach before any evaluator change.

**Status:** PROPOSED. Nothing here has been applied. No pin, config, evaluator constant, or historical record was changed. The sandbox precondition is still unmet, so no benchmark was run.

Companion record: `benchmarks/real_bugs/unresolved/freeze-records/vimj-fee-balance-v1-freeze-record-2026-11-17.json`

## 1. What is verified (read-only, this session)

| Item | Result |
|---|---|
| Freeze file committed in `066bcf8` (2026-11-09, "Align unresolved evaluation with operator freeze") | yes, the only commit that touches it |
| Committed bytes equal the working-tree file | yes (blob `f94c3d86…`, 919 bytes) |
| Committed file sha256 | `921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc` |
| Structural content (baseline commit, test sha, allowed paths, zero attempts, zero provider calls, no evaluator-only terms) | all pass |
| VIMJ checkout `6490833d…` clean; `fees.py` sha `6d131b6b…` | pass |
| Regression overlay `c77b210c…` | pass |
| Overlaid regression test: canonical `c5dd6217…`, raw CRLF `1bd8dcbe…` | both match their pins |
| Expected baseline failure (`125.00 != 175`, `1 failed`) | **not re-executed** (sandbox blocked). It matches the saved 2026-11-09 preflight records. |

## 2. The old pin is unreproducible

`9c9f1e6e…` is written into the evaluator, the frozen configs, and the docs. No committed bytes produce it. Tested variants that did not match: raw, LF/CRLF, trailing newline, minified, and several indentation and key-order variants (full list in the record).

The 2026-11-09 preflight passed the same freeze gate. That suggests the operator's original file hashed to `9c9f1e6e…` at the time, and the committed bytes were later different. This is an inference. The original file is not in the repository.

**The historical pin is preserved as-is.** Both hashes are recorded.

## 3. Options for your decision

**Option A (recommended): new versioned freeze, leave history alone.**
- Add `vimj-fee-balance-v2.json` and `vimj-fee-balance-v2-model-visible-freeze.json`. The v2 freeze file is the committed bytes (`921fe894…`).
- Add v2 Gemini budget and context configs, pinned to the v2 hashes.
- In `unresolved_eval.py`, add a `FREEZE_VERSION` selector. Keep every v1 constant. Default to v1 so the historical behaviour is unchanged. Opt into v2 explicitly.
- Document that v1's pin is unreproducible and that v2 is the first version pinned to committed bytes.
- Files touched: `unresolved_eval.py` (new constants and selector), three new JSON files, docs, and one test file. The historical configs are not modified.

**Option B: re-pin v1 to `921fe894…` in place.** Rejected. It rewrites the historical config hashes and the evaluator's history. It conflicts with "do not modify the historical 2026-11-09 pin."

**Option C: obtain the original operator bytes.** If you have the original file that hashes to `9c9f1e6e…`, supply it. We can then decide which version is authoritative and document the discrepancy. This is the strongest evidence, but it may not exist anymore.

## 4. Proposed changes under Option A (not applied)

```diff
 # benchmarks/real_bugs/unresolved_eval.py (sketch)
+FREEZE_VERSIONS = {
+    "v1": {  # historical; pin unreproducible; unchanged
+        "freeze_path": "unresolved/vimj-fee-balance-v1-model-visible-freeze.json",
+        "model_visible_freeze_sha256": "9c9f1e6e71ce96925c0c716dd452ccec2e1254241e523632ec4226701b7555f6",
+    },
+    "v2": {  # proposed; committed bytes
+        "freeze_path": "unresolved/vimj-fee-balance-v2-model-visible-freeze.json",
+        "model_visible_freeze_sha256": "921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc",
+    },
+}
```

Exact v2 config hashes (`freeze_sha256`, budget, context) would be computed only after v2 file content is final. They are not computed yet, because the freeze content and evaluation IDs are a decision for you.

## 5. What I need from you

1. **Pick an option:** A (recommended), B (rejected above), or C (supply original bytes first).
2. **Confirm v2 identifiers,** such as the evaluation ID. I suggest `vimj-offline-candidate-2026-11-17`, with the same challenge content.
3. **Confirm the record can be marked authoritative** after the approved option is implemented. Until then it stays `PROPOSED_PENDING_OWNER_REVIEW`.

Nothing else is blocked on this. The remaining blockers are the sandbox process limit (884 tasks, must drop below 60), Anthropic permission, and the unwired `claude_cli` evaluator path.

## 6. Separation of evidence

- The 37 adapter tests (`tests/test_claude_cli_provider.py`) check adapter logic with fake executables. They are **not** evidence of a VIMJ repair.
- The scripted fixture (`vimj-offline-scripted-fee-balance-v1.patch`) is operator-authored, not AI-generated, and has no post-patch result yet.
- No benchmark result exists yet for this case.
