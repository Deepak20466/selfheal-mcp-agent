# Manual bug reports

The scanner only creates findings for the categories it knows how to detect. A logic bug found by
human review, or a bug in an untested repository that produces no scanner finding, can therefore
have no row to pass to `selfheal fix`. `report-bug` creates a `manual` finding for an already
registered repository:

```powershell
selfheal report-bug --repo owner/repo --file src/app.py --description "Previous page on page 1 should do nothing"
selfheal fix owner/repo 42 --suggest
```

Use the finding ID printed by the first command in place of `42`. The repository must be registered
and included in the existing exact repository allowlist. In-repository app paths must remain under
that app's configured write prefixes. The service validates relative paths and applies the existing
patch policy to any eventual diff. It scrubs descriptions with the existing text secret/PII scrubber
before storage.

Manual findings can only enter suggestion mode. A suggestion PR is labeled `unverified-suggestion`,
states that the change is UNVERIFIED, runs no verification, requires human review, and is never
auto-merged. The regular verified repair route rejects manual findings. Reporting a bug does not
run a scanner, test suite, provider, or repair by itself.
