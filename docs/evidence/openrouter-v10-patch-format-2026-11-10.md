# OpenRouter patch-format follow-up

## Root-cause evidence

The published attempt for `attrs-73dc0d2e2860` records a 495-byte patch candidate and the exact
validation error `Diff does not touch any files`. The raw JSONL does **not** include the candidate
text. The only supportable root-cause statement is that the path parser found no paths. It is not
possible to determine from this artifact whether file headers were missing, formatting included
Markdown, or another unsupported layout caused that result. No replacement patch is inferred.

The benchmark previously stored candidate byte/line counts but discarded its content. New attempts
now retain the exact candidate and SHA256 when it is at most 64 KiB and the secret scrubber makes
no changes. Otherwise they retain its digest and an omission reason.

## Format and safety changes

The patch path now accepts and normalizes only:

- one exact enclosing `diff`/`patch` Markdown fence;
- a bare unified diff with a matching `---`/`+++` pair;
- a single `diff --git a/path b/path` with matching file headers; or
- a single `diff --git` path plus a valid hunk but no file-header pair, for which the two
  structural headers are synthesized without editing hunk lines.

Prose wrappers, partial or conflicting headers, multiple files, additions/deletions, path
traversal, paths outside the frozen source list, and malformed hunks are rejected. The normalized
candidate still goes through the MCP sandbox and `git apply --check` in the isolated checkout.
Regression and full-suite verification requirements are unchanged.

OpenRouter case limits now use the smaller of remaining daily allowance and the 10-request case
cap. If that total is at least two, the first repair phase may use all but one request and the one
correction phase may use the reserved request. The meter continues enforcing the original total
cap.

## Offline verification

The offline tests cover a no-file-header reproducer for the recorded no-path parser condition,
accepted format variants, malformed and unsafe forms, one correction, and request-budget
arithmetic. An isolated temporary
Git checkout starts with a failing regression; a normalized bare unified diff passes `git apply
--check`, applies, and makes the regression pass. This is a synthetic verification fixture, not a
replay of the historical model output because the original bytes were not published.

No provider request or database connection was made for this follow-up. The frozen case data and
historical attempt rows remain unchanged.
