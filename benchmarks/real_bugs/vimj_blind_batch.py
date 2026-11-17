"""Repeated blind single-shot Claude repair trials across cases and models.

Every trial is an independent, fresh `claude -p` process (see vimj_blind_claude_generate). The
operator's claude.ai subscription login is used; no API key is read or forwarded. A usage-limit
or auth error stops the batch and is recorded rather than retried. Patches are only generated
here; they are verified separately inside Bubblewrap by vimj_bench.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmarks.real_bugs import claude_cli_provider as cli
from benchmarks.real_bugs import vimj_blind_claude_generate as gen
from benchmarks.real_bugs.vimj_cases import get_case

PROVENANCE = (
    "Blind single-shot repair from a fresh Claude Code CLI process (model alias {model}, claude.ai "
    "subscription login, no API key), no tools, no repository access, no prior-patch exposure"
)


def run_batch(candidate_root: Path, case_id: str, plan: list[tuple[str, int]], out: Path) -> int:
    case = get_case(case_id)
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    stopped = False
    for model, runs in plan:
        for index in range(1, runs + 1):
            if stopped:
                break
            label = f"{model}-run{index}"
            trial_dir = out / case_id / label
            if trial_dir.exists() and any(trial_dir.iterdir()):
                continue  # never overwrite an earlier trial
            try:
                record = gen.generate(candidate_root, model, trial_dir, case)
            except (cli.ClaudeCLIProviderError, ValueError, KeyError) as exc:
                errors.append({"label": label, "error": f"{type(exc).__name__}: {exc}"[:300]})
                stopped = isinstance(exc, (cli.ClaudeCLIUsageLimitError, cli.ClaudeCLIAuthError))
                print(f"[{label}] ERROR {type(exc).__name__}")
                continue
            patch = trial_dir / "blind.patch"
            if record.get("return_code") != 0 or not patch.exists():
                errors.append({"label": label, "error": "no patch produced"})
                print(f"[{label}] no patch")
                continue
            print(f"[{label}] patch {record['patch_sha256'][:12]} paths={record['patch_paths']}")
            entries.append(
                {
                    "label": label,
                    "patch": str(patch),
                    "recount": True,
                    "provenance": PROVENANCE.format(model=model),
                    "claim_level": "blind_single_shot_claude_cli_subscription",
                    "generation_record": str(trial_dir / "generation.json"),
                }
            )
    manifest = out / f"manifest-{case_id}.json"
    manifest.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    (out / f"errors-{case_id}.json").write_text(json.dumps(errors, indent=1), encoding="utf-8")
    print(f"{case_id}: {len(entries)} patches, {len(errors)} errors, manifest {manifest}")
    return 0 if entries else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--plan", required=True, help="e.g. sonnet:3,opus:2,haiku:2")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plan = [(m, int(n)) for m, n in (item.split(":") for item in args.plan.split(","))]
    return run_batch(args.candidate_root, args.case, plan, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
