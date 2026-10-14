# Using agent-canary in CI

Run the experiment on a schedule or when agent configuration changes, and fail the job when an injected instruction is followed.

```yaml
name: agent-canary
on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:

jobs:
  canary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install .
      - name: Run canaries
        env:
          AGENT_API_KEY: ${{ secrets.AGENT_API_KEY }}
        run: |
          agent-canary run \
            --cmd "/usr/local/bin/my-agent --non-interactive {prompt}" \
            --pass-env AGENT_API_KEY \
            --runs 10 --jobs 3 --timeout 600 \
            --format markdown --out results.json | tee -a "$GITHUB_STEP_SUMMARY"
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: canary-results
          path: results.json
```

Notes:

- Use an absolute path for the agent binary. The agent runs inside a temporary repository.
- Give the job a dedicated, low-privilege API key. A GitHub-hosted runner is a fresh VM, which is the kind of disposable environment this tool expects.
- Exit code 1 fails the job when a channel was followed. Exit code 3 (inconclusive) also fails it, which is intended: it means the agent did not run.
- `--max-rate 0.1` tolerates occasional follows if you decide that is acceptable. Pick the threshold deliberately and write it down.
- To compare against a stored baseline, keep an earlier `results.json` as an artifact and run `agent-canary compare baseline.json results.json`.
- Cost scales with channels x runs. Use `--dry-run` first.
