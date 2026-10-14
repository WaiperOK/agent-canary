from __future__ import annotations

from collections import defaultdict

from .runner import RunResult
from .stats import wilson


def summarize(results: list[RunResult]) -> list[dict]:
    by_channel: dict[str, list[RunResult]] = defaultdict(list)
    for r in results:
        by_channel[r.channel].append(r)
    rows = []
    for channel, items in by_channel.items():
        n = len(items)
        k = sum(1 for r in items if r.followed)
        lo, hi = wilson(k, n)
        rows.append({
            "channel": channel, "followed": k, "runs": n, "rate": k / n if n else 0.0,
            "ci_low": lo, "ci_high": hi,
            "timeouts": sum(1 for r in items if r.timed_out),
            "crashes": sum(1 for r in items if r.exit_code not in (0, None)),
        })
    return rows


def format_table(rows: list[dict]) -> str:
    header = f"{'channel':<16}{'followed':<10}{'rate':<8}{'95% interval':<18}{'timeouts':<10}{'nonzero exit':<12}"
    lines = [header, "-" * len(header)]
    for r in rows:
        interval = f"[{r['ci_low']:.0%}, {r['ci_high']:.0%}]"
        lines.append(f"{r['channel']:<16}{str(r['followed']) + '/' + str(r['runs']):<10}{r['rate']:<8.0%}{interval:<18}{r['timeouts']:<10}{r['crashes']:<12}")
    return "\n".join(lines)


def warnings(rows: list[dict]) -> list[str]:
    notes = []
    quiet = [r for r in rows if r["followed"] == 0 and r["runs"] < 20]
    if quiet:
        runs = min(r["runs"] for r in quiet)
        bound = max(r["ci_high"] for r in quiet)
        notes.append(
            f"{len(quiet)} channel(s) had no follows. With {runs} run(s) each, that only bounds the true rate below {bound:.0%}. "
            "'ignored' is not 'safe'; use more runs for a tighter bound."
        )
    broken = [r["channel"] for r in rows if r["timeouts"] + r["crashes"] > r["runs"] / 2]
    if broken:
        notes.append(
            f"{len(broken)} channel(s) had more than half of their runs time out or exit non-zero. Check your --cmd before trusting these numbers. "
            "The agent runs inside the temporary repository, so use absolute paths for scripts and binaries."
        )
    return notes


def format_markdown(rows: list[dict]) -> str:
    lines = ["| Channel | Followed | Rate | 95% interval | Timeouts | Non-zero exit |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| `{r['channel']}` | {r['followed']}/{r['runs']} | {r['rate']:.0%} | [{r['ci_low']:.0%}, {r['ci_high']:.0%}] | {r['timeouts']} | {r['crashes']} |")
    return "\n".join(lines)


def format_json(rows: list[dict]) -> str:
    import json
    return json.dumps(rows, indent=2)


FORMATTERS = {"table": format_table, "markdown": format_markdown, "json": format_json}


def compare(a: list[RunResult], b: list[RunResult]) -> list[dict]:
    """Per-channel comparison of two experiments with a two-sided Fisher exact test."""
    from .stats import fisher_exact
    left = {r["channel"]: r for r in summarize(a)}
    right = {r["channel"]: r for r in summarize(b)}
    rows = []
    for channel in sorted(set(left) | set(right)):
        la, rb = left.get(channel), right.get(channel)
        row = {"channel": channel, "a": None, "b": None, "p_value": None}
        if la:
            row["a"] = (la["followed"], la["runs"])
        if rb:
            row["b"] = (rb["followed"], rb["runs"])
        if la and rb:
            row["p_value"] = fisher_exact(la["followed"], la["runs"] - la["followed"], rb["followed"], rb["runs"] - rb["followed"])
        rows.append(row)
    return rows


def format_compare(rows: list[dict], label_a: str = "A", label_b: str = "B") -> str:
    def cell(x):
        return "-" if x is None else f"{x[0]}/{x[1]} = {x[0] / x[1]:.0%}"

    header = f"{'channel':<16}{label_a:<16}{label_b:<16}{'p-value':<10}"
    lines = [header, "-" * len(header)]
    for r in rows:
        p = "-" if r["p_value"] is None else f"{r['p_value']:.3f}"
        lines.append(f"{r['channel']:<16}{cell(r['a']):<16}{cell(r['b']):<16}{p:<10}")
    return "\n".join(lines)


def compare_notes(rows: list[dict]) -> list[str]:
    notes = []
    differing = [r["channel"] for r in rows if r["p_value"] is not None and r["p_value"] < 0.05]
    if differing:
        notes.append(f"Differences with p < 0.05 in: {', '.join(differing)}. With few runs and many channels, some of these can be chance; confirm with more runs.")
    elif any(r["p_value"] is not None for r in rows):
        notes.append("No channel shows a difference at p < 0.05. With small samples this does not mean the configurations behave the same.")
    return notes
