# VIMJ freeze V2: implementation report (2026-11-17)

**Written for:** the repository owner, to review what changed and what was proven.

**Scope:** implements the approved Option A. This is the freeze identity and offline validation only. **No benchmark was run.** The baseline regression was **not re-executed** (sandbox blocked by the per-user process cap, 723 tasks now, target under 60). The expected baseline failure remains supported only by the saved 2026-11-09 preflight records.

**Authority:** V2 is marked authoritative in `benchmarks/real_bugs/unresolved/freeze-records/vimj-fee-balance-v2-freeze-record-2026-11-17.json`, after all implementation checks and focused tests passed (section 3). That authority covers the freeze identity and hash chain only.

## 1. Changed and created files

### Modified (SelfHeal repo)

| File | Change | sha256 after |
|---|---|---|
| `benchmarks/real_bugs/unresolved_eval.py` | Adds `FreezeVersion` (V1 built from the existing constants; V2 new), `DEFAULT_FREEZE_VERSION = "v1"`, `--freeze-version {v1,v2}`, version-aware validators and workspace preparation. V1 constants, V1 default, and V1 behaviour are unchanged. Also carries the earlier `provider_name` parameter (default `gemini_api`). | `98abad1acdcdae53d6536107dc4d3c764d4b954bc28e01958b78bc8e9b223425` |
| `benchmarks/real_bugs/offline_scripted_eval.py` | Adds `--freeze-version` (default v1). Gate checks take a version. Diagnostic only. | `5a9a97c6c788e1367ddb8fb4cea9c814397cecc3d4975fd4ebef1da246935898` |

### Created: V2 configs (`benchmarks/real_bugs/unresolved/`)

| File | sha256 |
|---|---|
| `vimj-fee-balance-v2-model-visible-freeze.json` (byte-identical to the committed `066bcf8` file) | `921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc` |
| `vimj-fee-balance-v2.json` (challenge) | `b67eebaea953c1393d82961c6b8376456ed149c34852b7e35ba8ce4b000ca24f` |
| `vimj-gemini-2.5-flash-budget-v2.json` | `786952c696095481fddc40b08a1d47c80f939e1ebf4051f2ecc65895fad62818` |
| `vimj-gemini-2.5-flash-context-v2.json` | `7eaa23bcc4a3e7540cb89573b08e262dfc61977d2cbfc72ddbe33c3c60c5eb66` |

### Created: records, docs, tests (evidence kept separate)

| File | sha256 |
|---|---|
| `benchmarks/real_bugs/unresolved/freeze-records/vimj-fee-balance-v2-freeze-record-2026-11-17.json` (authoritative V2 record) | `7e37f0f2d31aece52adacb261adea4a978bd40771bf52044d381683ffc41f54b` |
| `docs/vimj-freeze-versions.md` (new doc; the existing unresolved-evaluation doc is not edited) | `aeea29808c9d85a0ad469ccf4fd1545594d5dd328bd7c8329cb173e638b992d5` |
| `tests/test_vimj_freeze_versions.py` (39 focused offline tests) | `a7c385d82f36663627089305a16084b615bc2ee7f67e2696374b7519a716cb14` |
| `benchmarks/real_bugs/evidence/vimj-freeze-v2-implementation-2026-11-17.md` (this report) | (see the file) |

### Earlier, unchanged in this step (adapter, kept separate)

`benchmarks/real_bugs/claude_cli_provider.py` (`1f9cb7f7e35702652e710e09156450563b6044a675f15ad77fceebfc2b1dad87`) and `tests/test_claude_cli_provider.py` (`04556f6c3c1f190ad2a8f93d2eeb5cbeac71e1b0d6c1e202c482f910de4d28bb`), with 37 tests. These check adapter logic only. **They are not VIMJ repair evidence.**

### Not created or modified (by design)

- The 2026-11-17 proposal record `vimj-fee-balance-v1-freeze-record-2026-11-17.json` is unchanged. Its status stays `PROPOSED_PENDING_OWNER_REVIEW`, and the V2 record supersedes it for selection.
- The existing unresolved-evaluation doc `docs/unresolved-bug-evaluation.md` is unchanged.
- The VIMJ clean checkout, production data, databases, allowlist, and provider settings are unchanged.
- MAX_PROCESSES, Bubblewrap, and the sandbox are unchanged.

## 2. V1 unchanged: proof

A full-SHA-256 manifest of every V1 artifact was taken **before** any edit and again **after**. Scope: `benchmarks/real_bugs/unresolved/`, `benchmarks/real_bugs/results/unresolved/`, `docs/unresolved-bug-evaluation.md`, `benchmarks/real_bugs/unresolved_eval.py`, `tests/test_unresolved_real_bug_eval.py`.

Result of `comm` between the two manifests:

- **Byte-identical, every pre-existing V1 file:** V1 challenge `f586130a…`, V1 freeze `921fe894…`, V1 overlay `c77b210c…`, V1 budget `9d40775a…`, V1 context `e4156ca6…`, every historical result file, the unresolved-evaluation doc, and the existing evaluator test file.
- **Changed:** only `unresolved_eval.py` (its source changed; its V1 constants are preserved and tested, section 3).
- **Added:** the four V2 config files, one of which is the V2 freeze copy. Nothing existing was removed.

The V1 pin `9c9f1e6e…` is still in the V1 files and constants, unchanged. Under V1, the freeze gate still refuses the checked-in file without the diagnostic override, as before.

## 3. Test results

| Suite | Scope | Result |
|---|---|---|
| `tests/test_vimj_freeze_versions.py` | V1 immutability, default and explicit selection, unknown version fails closed, all V2 full-hash pins, all V2 cross-references, V2 identities distinct from V1, V2 gate passes, altered freeze and altered challenge fail closed, challenge mutations, V1/V2 cross-version isolation, no V2 identity leaks into V1 files | **39 passed** |
| `tests/test_claude_cli_provider.py` (separate evidence) | Adapter logic with fake executables | **37 passed** |
| `tests/test_unresolved_real_bug_eval.py -k "frozen_config_and_model_prompt or budget_is_versioned or gemini_live_gate"` (V1 compatibility) | Existing frozen-config and budget tests, selected by name | **3 passed**, 20 deselected |
| `ruff check` (changed and new Python files) | Lint | **All checks passed** |
| `ruff format --check` (changed Python files) | Format | **3 files already formatted** |

Commands run (offline; no sandbox, database, or model):

```bash
~/.venvs/selfheal/bin/python -m pytest --noconftest -p no:cacheprovider tests/test_vimj_freeze_versions.py tests/test_claude_cli_provider.py -q
~/.venvs/selfheal/bin/python -m pytest --noconftest -p no:cacheprovider tests/test_unresolved_real_bug_eval.py -q -k "frozen_config_and_model_prompt or budget_is_versioned or gemini_live_gate"
~/.venvs/selfheal/bin/ruff check <changed files> && ~/.venvs/selfheal/bin/ruff format --check <changed files>
```

**Not run:** the evaluator's offline path under `--freeze-version v2`. Its first step runs the frozen baseline in the Bubblewrap sandbox, which is blocked, so it stays unrun until the precondition is met. The rest of the existing evaluator test file was not run, because its other tests need MCP, git, or database fixtures.

## 4. Verification of cross-references

Every V2 chain link was checked against full SHA-256 values by the tests (section 3):

- Freeze → challenge: `model_visible_freeze_sha256` equals the freeze file's hash.
- Challenge → budget: `challenge_freeze_sha256` equals the challenge file's hash.
- Freeze → budget: `model_visible_freeze_sha256` equals the freeze file's hash.
- Budget → context: `budget_config_sha256` equals the budget file's hash.
- Identities: challenge ID, budget ID, and context ID match across files and the evaluator's V2 constants.
- Source: `model_visible_freeze_source_commit` equals `066bcf8a…`, and the freeze file's inner ID `vimj-offline-candidate-2026-11-09` is recorded.
- Overlay: V2 reuses the V1 overlay, pinned at `c77b210c…`.

## 5. Caveats

- The V2 freeze file's inner `evaluation_id` still says 2026-11-09. Changing it would change the pinned bytes, so it stays. The V2 challenge config records the source explicitly.
- `unresolved_eval.py` changed as a file. Its V1 values were preserved and are tested. Its file hash therefore differs from the pre-change manifest.
- The scripted harness's default is still V1. Its output stays labelled as a scripted fixture, not AI-generated.
- Nothing in this step makes a provider or model call.

## 6. Remaining blockers before any VIMJ run

1. Bubblewrap sandbox with fewer than 60 tasks for the executing user (`MAX_PROCESSES` stays 64).
2. Re-executed baseline regression in the sandbox, recorded before any patch test.
3. Anthropic permission decision before any Claude-powered run.
4. Evaluator `--provider claude_cli` wiring is not done. Its freeze and budget configs are Gemini-named, and the Claude path still needs its own configs and gates.
