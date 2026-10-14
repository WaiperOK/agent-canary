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
