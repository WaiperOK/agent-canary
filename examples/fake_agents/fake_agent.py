"""A stand-in for a real agent, used by the tests and the README demo.

It reads every file in the repository (decoding invisible tag characters first, like a tokenizer would)
and creates the canary files that some instructions ask for, depending on its personality:

  obedient   follows every channel
  selective  follows only the channels listed after --only
  ignorant   follows nothing
  crash      exits with status 3
  sleep      hangs, spawning a child process and writing its pid to the path in argv[1]
  record     appends every canary file name it sees to the path in argv[1]
  env        records whether a secret environment variable is visible (writes ENV_SEEN.txt)
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PATTERN = re.compile(r"create an empty file named (CANARY_[a-z_]+_[0-9a-f]+\.txt)")


def decode_tags(text: str) -> str:
    return "".join(chr(ord(c) - 0xE0000) if 0xE0020 <= ord(c) <= 0xE007E else c for c in text)


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "obedient"
    root = Path.cwd()
    if mode == "crash":
        return 3
    if mode == "sleep":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        if len(argv) > 1:
            Path(argv[1]).write_text(str(child.pid))
        time.sleep(600)
        return 0
    if mode == "record":
        seen = []
        for path in root.rglob("*"):
            if path.is_file():
                seen += PATTERN.findall(decode_tags(path.read_text(encoding="utf-8", errors="ignore")))
        with open(argv[1], "a", encoding="utf-8") as fh:
            fh.write("\n".join(seen) + "\n")
        return 0
    if mode == "env":
        (root / "ENV_SEEN.txt").write_text("yes" if "CANARY_TEST_SECRET" in os.environ else "no")
        return 0
    if mode == "ignorant":
        return 0
    only = set(argv[2].split(",")) if mode == "selective" and len(argv) > 2 and argv[1] == "--only" else None
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        text = decode_tags(path.read_text(encoding="utf-8", errors="ignore"))
        for name in PATTERN.findall(text):
            channel = name[len("CANARY_"):name.rindex("_")]
            if only is None or channel in only:
                (root / name).touch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
