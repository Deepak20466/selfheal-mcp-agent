# VIMJ fee-balance offline benchmark: diagnostic run report (2026-11-17)

**Written for:** the repository owner, to decide what can be claimed and what must be fixed before a real run.

## Verdict

**This is not a benchmark result.** No repair was validated and no regression was observed to pass.

- The offline pipeline could not complete its baseline step. The OS test sandbox refused to start, and the evaluator correctly stopped instead of running VIMJ tests outside the sandbox.
- The only patch in the run is an **operator-authored scripted fixture** (`output_source: scripted_fixture_not_model_generated`). It is not model output, so it says nothing about whether a model can find or fix the defect.
- No provider or model request was made. No Claude Pro quota was used. Live execution is disabled.

## 1. VIMJ baseline (verified)

| Item | Value |
|---|---|
| Checkout | `~/vimj-baseline-clean-6490833` (clone of `Deepak20466/vimj-studio-os-saas`) |
| HEAD | `6490833d178b71624d024b16046240a20a3f78a6` (matches required baseline) |
| `git status --porcelain --ignored` | empty (clean) |
| `backend/app/routers/fees.py` sha256 | `6d131b6b91d079f61af2d78b1e80e54ab9512093951b8caae0ff5a4735d09d28` (matches pin) |
| `backend/tests/test_requested_changes.py` (baseline, pre-overlay) sha256 | `6df60a58271c54a5e54c8db0d46390d8982c947f8ba250ad778423a27f9db9f1` (expected; the overlay is what adds the frozen test) |
| Frozen regression test | `tests/test_requested_changes.py::RequestedChanges::test_updating_unpaid_fee_amount_keeps_outstanding_balance_in_sync` |
| Frozen test, after overlay (LF-normalised / canonical) | `c5dd62175204e73a82cd3741178debf0d71a340080e17187a371d3986242e67a` (matches pin) |
| Frozen test, raw bytes with CRLF line endings | `1bd8dcbec1edfa404723df1ecbfc496dc14eabfcbbcd37a814e90733f447a007` (matches the raw pin; the checkout is LF, so the pin reflects the original CRLF form) |
| Overlay patch `vimj-fee-balance-v1-test.patch` | `c77b210cef093b15370493e96daf76219f5f16c3f13224a26b955a40e70c61d4` (matches pin) |

Defect read from source: `update_fee` (`app/routers/fees.py`, ~line 134) sets `amount` but never recomputes `balance_amount` for unpaid fees, so the test sees 125 instead of 175.

## 2. Evaluator integrity gates

| Gate | Result |
|---|---|
| Model-config structural checks (`validate_model_config`) | passed |
| Gemini budget config (`validate_budget_config`, hash) | passed |
| Gemini context policy (`validate_context_policy`, hash) | passed |
| Overlay hash, freeze JSON hash, scripted fixture hash | passed |
| **Model-visible freeze FILE hash** | **MISMATCH**: file is `921fe894efc6a20f85185c76d4bfae77d01c65f273699564f2ece151a3ab3efc`; pinned `9c9f1e6e71ce96925c0c716dd452ccec2e1254241e523632ec4226701b7555f6` |

The mismatch is **unexplained**. I tested LF/CRLF, trailing-newline, minified, and re-indented variants of the file. None reproduces the pin. The file's contents do match the challenge (same baseline commit, test hash, and allowed paths). Because of the mismatch, the official evaluator refuses to run. For this diagnostic only, the harness takes an explicit override flag and marks the record `official: false`. I did not change any pinned value.

## 3. Baseline regression result: NOT OBTAINED

The evaluator's baseline step runs the frozen test inside a Bubblewrap sandbox. The sandbox failed to start:

```
bwrap: Creating new namespace failed: Resource temporarily unavailable
```

Cause (measured):

- `mcp_server/execution_sandbox.py` sets `RLIMIT_NPROC` to `MAX_PROCESSES = 64`. On Linux this limit counts every process and thread owned by the user, not just sandboxed ones.
- The desktop session for this user had **891 tasks** (threads included) at the time of the run, and still **868** after the database was stopped.
- A minimal `bwrap --unshare-user ... true` succeeds. The failure is specific to the sandbox's process limit.

The evaluator then reported `baseline failed for an unexpected reason`, which is the correct stop. I did not raise the limit, add a native fallback, or run VIMJ test code outside the sandbox. Those would weaken a documented containment control, and that decision belongs to the repository owner.

## 4. Scripted fixture (NOT model-generated)

- Fixture: `benchmarks/real_bugs/unresolved/vimj-offline-scripted-fee-balance-v1.patch`
- sha256: `33882294faf5d508e1ff00dc1acbb637487774c2ba25f92656a5df07a760d7ee`
- Content: one hunk in `app/routers/fees.py`, 4 added lines. After the `setattr` loop, when `amount` or `product_amount` changes without `balance_amount` and the fee is not PAID, it sets `balance_amount = amount + (product_amount or 0)`.
- Written by the operator/assistant after reading the source. **Not produced by any language model.**

Scripted reply sequence (`ScriptedProvider`, no network): `read_file` → `run_tests` (expected fail) → `propose_patch` (fixture) → `run_tests` (expected pass) → final text. Usage figures were approximate (characters ÷ 4). The arm never ran, so no usage was recorded.

## 5. What was verified about the fixture (no repo code executed)

- `git apply --check` on a scratch copy of the candidate's `app/` tree: **OK**.
- `git apply` on that scratch copy: `app/routers/fees.py | 4 ++++`.
- `ast.parse` of the patched `fees.py`: **parses**. This is syntax only. The code was not run.

## 6. Post-patch regression result: NOT OBTAINED

The arm and the independent re-check both execute repository tests through the same sandbox, which is blocked (section 3). **I have no evidence that the fixture makes the frozen regression pass.** Expected behaviour from reading the code is not a result and is not claimed.

## 7. Baseline integrity (before vs after all runs)

| Check | Before | After |
|---|---|---|
| HEAD | `6490833d…a24a20a3f78a6` | identical |
| `git status --porcelain --ignored` lines | 0 | 0 |
| Full-tree sha256 (sorted file hashes, `.git` excluded) | `468b996be6d3313adf580de55b98a5d01ac46f241acaf6b4572ba50a81f1d9b4` | identical |

The clone was only read. Staging copies live under `~/.cache/selfheal/real-bugs/prepared/`, outside the clone and the repo.

## 8. Claude CLI adapter (offline only)

Files:

- `benchmarks/real_bugs/claude_cli_provider.py`: adapter. Subscription-login gate (`claude.ai` + `pro` + `firstParty`), no API key forwarded, `--tools ""`, no MCP, no session persistence, cost `None`, usage recorded as **unmetered and unverified**.
- `tests/test_claude_cli_provider.py`: 37 offline tests. Fake executables stand in for the CLI. Also checks that Gemini remains the evaluator default and that the Gemini pinned hashes still validate.

Result: **37 passed** (`python -m pytest --noconftest -p no:cacheprovider tests/test_claude_cli_provider.py`). Ruff check and format clean on the new files.

Not done: the evaluator does **not** accept `--provider claude_cli`. Wiring it requires Claude-specific budget and context configs with their own hashes, which I have not created. Live execution is therefore unreachable and disabled by default.

The $0 cap is **not enforced** for subscription usage. The CLI's `total_cost_usd` is an API-equivalent estimate; it is recorded only as `unverified_api_equivalent_estimate_usd`.

## 9. Files created or modified (SelfHeal repo)

Created:

- `benchmarks/real_bugs/claude_cli_provider.py`
- `benchmarks/real_bugs/offline_scripted_eval.py`
- `benchmarks/real_bugs/unresolved/vimj-offline-scripted-fee-balance-v1.patch`
- `tests/test_claude_cli_provider.py`
- `benchmarks/real_bugs/results/unresolved/vimj-offline-scripted-fee-balance-2026-11-17.jsonl` (stopped record, sandbox failure)
- `benchmarks/real_bugs/results/unresolved/vimj-offline-scripted-fee-balance-2026-11-17-rerun.jsonl` (stopped record, same cause)
- `benchmarks/real_bugs/evidence/vimj-offline-benchmark-report-2026-11-17.md` (this file)
- `ai_self_healing.egg-info/` and `.ruff_cache/` (build and lint caches from `pip install -e .` and `ruff`)

Modified:

- `benchmarks/real_bugs/unresolved_eval.py`: `_execute` gained `provider_name: str = "gemini_api"`, and the hard-coded `"gemini_api"` became that parameter. Default behaviour is unchanged. Snapshot of the original: `…/scratchpad/snap/unresolved_eval.py.orig`.

Outside the repo (caches and local infrastructure, safe to delete):

- `~/.venvs/selfheal` (SelfHeal dependencies), `~/.venvs/selfheal-offline` (pytest/ruff only; unused)
- `~/.cache/selfheal/real-bugs/venvs/vimj-studio-os-saas-unresolved-fee-balance` (VIMJ backend requirements, pytest)
- `~/.cache/selfheal/real-bugs/prepared/…` (staging copy from the failed baseline)
- `~/.cache/selfheal/pg16/` (PostgreSQL 16.16 server extracted from the PGDG `.deb`, data directory, password file `pw` with mode 600, and the `selfheal_real_bug_test` database). The cluster is **stopped** to free memory.

## 10. Exact commands executed

```bash
# Snapshot of the clean checkout (before and after)
git -C ~/vimj-baseline-clean-6490833 rev-parse HEAD
git -C ~/vimj-baseline-clean-6490833 status --porcelain --ignored | wc -l
sha256sum ~/vimj-baseline-clean-6490833/backend/app/routers/fees.py \
          ~/vimj-baseline-clean-6490833/backend/tests/test_requested_changes.py
(cd ~/vimj-baseline-clean-6490833 && find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum)

# Environments
python3 -m venv ~/.venvs/selfheal && ~/.venvs/selfheal/bin/pip install -q -e . pytest ruff
python3 -m venv --system-site-packages ~/.cache/selfheal/real-bugs/venvs/vimj-studio-os-saas-unresolved-fee-balance
~/.cache/selfheal/real-bugs/venvs/vimj-studio-os-saas-unresolved-fee-balance/bin/pip install -q \
    -r ~/vimj-baseline-clean-6490833/backend/requirements.txt pytest

# PostgreSQL 16 (server from apt.postgresql.org .deb, extracted without root)
apt-get download postgresql-16   # in ~/.cache/selfheal/pg16/deb, then dpkg -x into root/
initdb -D data -L root/usr/share/postgresql/16 -U selfheal --pwfile=pw \
       --auth-host=scram-sha-256 --auth-local=scram-sha-256
pg_ctl -D data -o "-p 5434 -h 127.0.0.1 -k ~/.cache/selfheal/pg16" -w start
/usr/bin/psql -h 127.0.0.1 -p 5434 -U selfheal -d postgres -c "CREATE DATABASE selfheal_real_bug_test;"

# Evaluator's own offline command (stopped at freeze-file hash gate; no evidence file written)
~/.venvs/selfheal/bin/python -m benchmarks.real_bugs.unresolved_eval \
    --candidate-root ~/vimj-baseline-clean-6490833 \
    --evidence benchmarks/real_bugs/results/unresolved/vimj-offline-baseline-2026-11-17.jsonl

# Scripted diagnostic (stopped at baseline sandbox failure), twice
REAL_BUG_DATABASE_URL="postgresql+asyncpg://selfheal:$(cat ~/.cache/selfheal/pg16/pw)@127.0.0.1:5434/selfheal_real_bug_test" \
~/.venvs/selfheal/bin/python -m benchmarks.real_bugs.offline_scripted_eval \
    --candidate-root ~/vimj-baseline-clean-6490833 \
    --evidence benchmarks/real_bugs/results/unresolved/vimj-offline-scripted-fee-balance-2026-11-17.jsonl \
    --diagnostic-allow-freeze-file-hash-mismatch

# Sandbox diagnosis (no repo code run)
cat /proc/sys/user/max_user_namespaces; bwrap --ro-bind / / --unshare-user --unshare-pid --dev /dev --proc /proc true
ps -u deepak -L --no-headers | wc -l

# Patch check on a scratch copy (git apply + ast only; nothing executed)
git apply --check benchmarks/real_bugs/unresolved/vimj-offline-scripted-fee-balance-v1.patch
python3 -c "import ast; ast.parse(open('app/routers/fees.py').read())"

# Adapter tests (mocked; no claude binary, no network) and lint
~/.venvs/selfheal/bin/python -m pytest --noconftest -p no:cacheprovider tests/test_claude_cli_provider.py -q
~/.venvs/selfheal/bin/ruff check <new files> && ~/.venvs/selfheal/bin/ruff format --check <new files>
```

## 11. Limitations and not executed

- **Not executed:** baseline regression, post-patch regression, full VIMJ backend suite, and the evaluator's arm loop. All are blocked by the sandbox process limit (section 3).
- **Not executed:** any real or mocked model reasoning step. The only reply source was the scripted fixture.
- **Not established:** whether the fixture fixes the test, and whether any model can identify the defect.
- **Not run by design:** `claude -p`, `claude setup-token`, any provider request, the live evaluator path, and any PR, push, or allowlist change.
- The official freeze-file hash mismatch and the unexplained pin remain open.

## 12. Remaining blockers for a live Claude-powered benchmark

1. **Sandbox process limit (`MAX_PROCESSES = 64`, per user).** The benchmark needs a session with fewer than ~60 tasks for this UID, or a dedicated low-process account. Raising the limit is a containment decision for the repository owner.
2. **Freeze-file hash.** The evaluator's pinned `9c9f1e6e…` does not match the file (`921fe894…`), and no explored variant explains it. Decide whether the file or the pin is authoritative, then re-freeze explicitly. Do not change either silently.
3. **Anthropic permission is unconfirmed** (see the earlier policy analysis). Consumer Terms §3 excludes automated access unless Anthropic explicitly permits it. Written confirmation is still pending.
4. **Evaluator wiring is absent.** `--provider claude_cli` needs its own budget and context configs, with hashes, a model pin, and the subscription gates (not yet written).
5. **CLI output shape unverified.** The `structured_output` field and `--max-turns 3` behaviour are inferred from the binary and must be confirmed by one approved, minimal live call.
6. **Model choice** (recommended `claude-sonnet-5-5`) and Pro quota are not yet decided.

## 13. Addendum: rerun attempt after a fresh-session request (2026-11-17, later)

Requested precondition: a fresh Linux session with fewer than 60 tasks, then a rerun.

**Outcome: not run.** The precondition is not met and the sandbox still fails. No baseline, scripted, or post-patch test was executed.

| Check | Result |
|---|---|
| Task count for UID `deepak` (`ps -u deepak -L`) | **884** (was 891 earlier; still far above 60) |
| Sandbox probe: `run_untrusted_command(['/bin/true'])` in an empty scratch directory | `passed: False`; `bwrap: Creating new namespace failed: Resource temporarily unavailable` |
| Cause | `RLIMIT_NPROC = 64` (`MAX_PROCESSES`) counts all tasks of the UID; unchanged |
| Clean checkout | HEAD `6490833d…`, `git status --porcelain --ignored` empty, full-tree sha256 `468b996be6d3313adf580de55b98a5d01ac46f241acaf6b4572ba50a81f1d9b4` (identical to the earlier snapshot) |

I did not raise `MAX_PROCESSES`, disable or bypass Bubblewrap, add a native fallback, start PostgreSQL, or make any model call. The Claude adapter tests remain separate evidence (37 passed) and are not proof of a VIMJ repair.

### Freeze-file investigation (Git history, read-only clone of the SelfHeal repo)

- The freeze file was introduced and last changed in exactly one commit: `066bcf8` (2026-11-09, "Align unresolved evaluation with operator freeze"). No other commit touches it.
- In that commit, the committed file hashes to **`921fe894…`**, which is also what is on disk today.
- In the same commit, the evaluator pins **`9c9f1e6e…`**, and `docs/unresolved-bug-evaluation.md` says that value is the SHA of the operator's freeze, "saved as" the committed path. The committed bytes do not produce that value.
- Byte variants of the committed file (LF/CRLF, trailing newline, minified, re-indented) do not produce `9c9f1e6e…` either.
- The freeze file's content is not an input to the model. `build_model_prompt()` is a hard-coded string. The file is only validated structurally (baseline commit, test hash, allowed paths, no evaluator-only fields), and those checks pass.
- The preflight evidence from 2026-11-09 records `model_visible_prompt_sha256 = daaaf447…`. The current `build_model_prompt()` gives `9fa8c944…`, so the prompt changed after that preflight and the historical prompt cannot be replayed exactly.

**Conclusion:** the repository cannot establish which value is authoritative. The only artifact in Git is the committed file (`921fe894…`). The pin `9c9f1e6e…` has no matching bytes anywhere in the repository or its history. The original operator file that produced the pin is not available here. I did not change the pin.

Decision needed from the repository owner. Either supply the original operator freeze bytes that hash to `9c9f1e6e…`, or approve a new, dated freeze record that pins the committed `921fe894…` and states that the 2026-11-09 pin was not reproducible. Either way, the change must be explicit and recorded, not silent.

### Remaining blockers, updated

1. Sandbox process cap: still blocked at 884 tasks. A rerun needs a dedicated low-process account or a fresh session that actually reaches fewer than 60 tasks. Those are the only options that keep the sandbox intact.
2. Freeze authority: decision above.
3. Anthropic permission, evaluator wiring, CLI output verification, and model choice: unchanged from section 12.
