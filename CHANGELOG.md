# Changelog

## 0.2.0

- five new channels: `vendor_doc`, `package_json`, `ci_comment`, `test_failure`, `commit_message` (12 in total)
- custom channels from a JSON file (`--channels-file`); paths are validated and confined to the repository
- `compare A.json B.json`: per-channel comparison of two configurations with a two-sided Fisher exact test
- `--label`, and results files now record tool version, time, command, prompt and channels
- `--jobs N` for parallel runs, `--dry-run` to see the cost first
- `--format markdown|json` for CI summaries and tooling
- `selftest` command; the stand-in agent now ships inside the package (it was previously only in the source tree, so `pip install` users could not use the demo)
- preflight notes about credentials the agent can reach through `HOME` and the environment
- every repository now has a one-commit git history when `git` is available
- the default task prompt now covers the new channels, so results are not comparable with 0.1.0

## 0.1.0

- initial release: seven channels, isolated and combined modes, fresh canary names, clean environment, no shell, process-group kill on timeout, Wilson intervals, `INCONCLUSIVE` for broken runs
