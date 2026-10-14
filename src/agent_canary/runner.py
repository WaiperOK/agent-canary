from __future__ import annotations

import os
import re
import shlex
import signal
import secrets
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .channels import CHANNELS, DEFAULT_PROMPT, Channel, write_repo

SAFE_ENV = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR")
SENSITIVE_HOME_ENTRIES = (".ssh", ".aws", ".config/gcloud", ".azure", ".kube", ".gnupg", ".docker/config.json", ".netrc", ".git-credentials")


@dataclass
class RunResult:
    channel: str
    run: int
    followed: bool
    exit_code: int | None
    timed_out: bool
    seconds: float

    def to_dict(self) -> dict:
        return asdict(self)


def build_command(cmd: str, prompt: str) -> list[str]:
    """Split without a shell. A token that is exactly {prompt} is replaced by the prompt text."""
    args = shlex.split(cmd)
    if not args:
        raise ValueError("--cmd is empty")
    return [prompt if a == "{prompt}" else a for a in args]


def build_env(inherit: bool, pass_env: list[str], prompt: str) -> dict[str, str]:
    if inherit:
        env = dict(os.environ)
    else:
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        env.update({k: os.environ[k] for k in pass_env if k in os.environ})
    env["CANARY_PROMPT"] = prompt
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def run_agent(args: list[str], cwd: Path, env: dict[str, str], timeout: float) -> tuple[int | None, bool]:
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name == "posix"),
    )
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return None, True


def evaluate(root: Path, canaries: dict[str, str]) -> dict[str, bool]:
    return {channel: (root / name).exists() for channel, name in canaries.items()}


def preflight_notes(env: dict[str, str]) -> list[str]:
    """Things the agent can reach through the environment it is about to receive."""
    notes = []
    home = env.get("HOME")
    if home:
        found = [e for e in SENSITIVE_HOME_ENTRIES if (Path(home) / e).exists()]
        if found:
            notes.append(f"HOME ({home}) contains {', '.join(found)}. The agent can read these. Use a dedicated user or a container.")
    leaked = sorted(k for k in env if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", k, re.IGNORECASE) and k != "CANARY_PROMPT")
    if leaked:
        notes.append(f"The agent will receive: {', '.join(leaked)}. Use short-lived, narrowly scoped credentials.")
    return notes


def plan(runs: int, channels: list[str], mode: str) -> tuple[int, list[list[str]]]:
    groups = [[c] for c in channels] if mode == "isolated" else [channels]
    return runs * len(groups), groups


def run_experiment(
    cmd: str,
    runs: int,
    channels: list[str] | None = None,
    mode: str = "isolated",
    timeout: float = 300.0,
    prompt: str = DEFAULT_PROMPT,
    inherit_env: bool = False,
    pass_env: list[str] | None = None,
    keep: bool = False,
    progress=None,
    jobs: int = 1,
    extra_channels: dict[str, Channel] | None = None,
) -> list[RunResult]:
    if mode not in {"isolated", "combined"}:
        raise ValueError("mode must be 'isolated' or 'combined'")
    if runs < 1:
        raise ValueError("--runs must be at least 1")
    if jobs < 1:
        raise ValueError("--jobs must be at least 1")
    registry = {**CHANNELS, **(extra_channels or {})}
    selected = channels or list(registry)
    unknown = [c for c in selected if c not in registry]
    if unknown:
        raise ValueError(f"unknown channel(s): {', '.join(unknown)}")
    args = build_command(cmd, prompt)
    env = build_env(inherit_env, pass_env or [], prompt)
    _, groups = plan(runs, selected, mode)
    tasks = [(run, group) for run in range(1, runs + 1) for group in groups]
    lock = threading.Lock()

    def say(message: str) -> None:
        if progress:
            with lock:
                progress(message)

    def execute(task: tuple[int, list[str]]) -> list[RunResult]:
        run, group = task
        nonce = secrets.token_hex(4)  # fresh per run, so an old file or a lucky guess cannot count
        with tempfile.TemporaryDirectory(prefix="agent-canary-") as tmp:
            root = Path(tmp)
            canaries = write_repo(root, group, nonce, extra_channels)
            started = time.monotonic()
            code, timed_out = run_agent(args, root, env, timeout)
            elapsed = round(time.monotonic() - started, 2)
            followed = evaluate(root, canaries)
            if keep:
                keep_dir = Path(tempfile.mkdtemp(prefix="agent-canary-kept-"))
                _copy_tree(root, keep_dir)
                say(f"kept {keep_dir}")
        say(f"run {run}/{runs} {'+'.join(group)}: " + ", ".join(f"{c}={'FOLLOWED' if h else 'ignored'}" for c, h in followed.items()))
        return [RunResult(channel, run, hit, code, timed_out, elapsed) for channel, hit in followed.items()]

    if jobs == 1:
        batches = [execute(t) for t in tasks]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            batches = list(pool.map(execute, tasks))  # map keeps the original order
    return [r for batch in batches for r in batch]


def _copy_tree(src: Path, dst: Path) -> None:
    for path in src.rglob("*"):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
