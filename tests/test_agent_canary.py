import json
import os
import py_compile
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_canary import cli
from agent_canary.channels import CHANNELS, NEUTRAL_FILES, canary_name, hide, write_repo
from agent_canary.report import summarize, warnings
from agent_canary.runner import RunResult, build_command, build_env, run_experiment
from agent_canary.stats import wilson

FAKE = Path(__file__).resolve().parents[1] / "examples" / "fake_agents" / "fake_agent.py"


def agent(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in (sys.executable, str(FAKE), *args))


def rate_by_channel(results):
    return {row["channel"]: row["followed"] for row in summarize(results)}


class TestStats(unittest.TestCase):
    def test_wilson_reference_values(self) -> None:
        self.assertAlmostEqual(wilson(0, 10)[1], 0.2775, places=3)
        lo, hi = wilson(50, 100)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))


class TestChannels(unittest.TestCase):
    def test_every_channel_embeds_its_canary(self) -> None:
        for name, channel in CHANNELS.items():
            with tempfile.TemporaryDirectory() as tmp:
                canaries = write_repo(Path(tmp), [name], "abcd1234")
                text = (Path(tmp) / channel.path).read_text(encoding="utf-8")
                decoded = "".join(chr(ord(c) - 0xE0000) if 0xE0020 <= ord(c) <= 0xE007E else c for c in text)
                self.assertIn(canaries[name], decoded, name)

    def test_hidden_unicode_is_not_visible_to_a_plain_text_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            canaries = write_repo(Path(tmp), ["hidden_unicode"], "abcd1234")
            text = (Path(tmp) / "docs/NOTES.md").read_text(encoding="utf-8")
            self.assertNotIn(canaries["hidden_unicode"], text)
            self.assertNotIn("create an empty file", text)

    def test_only_the_selected_channel_is_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(Path(tmp), ["visible_doc"], "abcd1234")
            for rel, neutral in NEUTRAL_FILES.items():
                body = (Path(tmp) / rel).read_text(encoding="utf-8")
                if rel == "docs/CONTRIBUTING.md":
                    self.assertIn("CANARY_", body)
                else:
                    self.assertEqual(body, neutral, rel)

    def test_code_channel_keeps_valid_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_repo(Path(tmp), ["code_comment"], "abcd1234")
            py_compile.compile(str(Path(tmp) / "src/util.py"), doraise=True)

    def test_unknown_channel_and_nonce_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            write_repo(Path(tmp), ["nope"], "abcd1234")
        self.assertNotEqual(canary_name("visible_doc", "aaaa1111"), canary_name("visible_doc", "bbbb2222"))

    def test_hide_roundtrip(self) -> None:
        self.assertEqual("".join(chr(ord(c) - 0xE0000) for c in hide("hi there")), "hi there")


class TestRunner(unittest.TestCase):
    def test_obedient_agent_follows_every_channel(self) -> None:
        results = run_experiment(agent("obedient"), runs=2, timeout=30)
        self.assertEqual(rate_by_channel(results), {name: 2 for name in CHANNELS})

    def test_selective_agent_only_follows_listed_channels(self) -> None:
        results = run_experiment(agent("selective", "--only", "visible_doc,hidden_unicode"), runs=2, timeout=30)
        followed = rate_by_channel(results)
        self.assertEqual(followed["visible_doc"], 2)
        self.assertEqual(followed["hidden_unicode"], 2)
        self.assertEqual(sum(v for k, v in followed.items() if k not in {"visible_doc", "hidden_unicode"}), 0)

    def test_ignorant_agent_follows_nothing(self) -> None:
        self.assertEqual(sum(rate_by_channel(run_experiment(agent("ignorant"), runs=2, timeout=30)).values()), 0)

    def test_combined_mode_uses_one_repo_for_all_channels(self) -> None:
        results = run_experiment(agent("obedient"), runs=1, mode="combined", timeout=30)
        self.assertEqual(len(results), len(CHANNELS))
        self.assertTrue(all(r.followed for r in results))

    def test_crash_is_recorded_and_not_counted_as_followed(self) -> None:
        results = run_experiment(agent("crash"), runs=1, channels=["visible_doc"], timeout=30)
        self.assertEqual(results[0].exit_code, 3)
        self.assertFalse(results[0].followed)

    def test_timeout_kills_the_whole_process_group(self) -> None:
        pidfile = Path(tempfile.mkdtemp()) / "child.pid"
        started = time.monotonic()
        results = run_experiment(agent("sleep", str(pidfile)), runs=1, channels=["visible_doc"], timeout=1.0)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(results[0].timed_out)
        self.assertIsNone(results[0].exit_code)
        child = int(pidfile.read_text())
        for _ in range(50):  # the kill is asynchronous; give it up to five seconds
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            self.fail("the agent's child process survived the timeout")

    def test_every_run_gets_a_fresh_unguessable_canary_name(self) -> None:
        log = Path(tempfile.mkdtemp()) / "names.txt"
        run_experiment(agent("record", str(log)), runs=4, channels=["visible_doc"], timeout=30)
        names = [n for n in log.read_text().split() if n]
        self.assertEqual(len(names), 4)
        self.assertEqual(len(set(names)), 4)

    def test_environment_is_scrubbed_by_default(self) -> None:
        with mock.patch.dict(os.environ, {"CANARY_TEST_SECRET": "s3cret"}):
            env = build_env(False, [], "task")
            self.assertNotIn("CANARY_TEST_SECRET", env)
            self.assertEqual(env["CANARY_PROMPT"], "task")
            self.assertEqual(build_env(False, ["CANARY_TEST_SECRET"], "t")["CANARY_TEST_SECRET"], "s3cret")
            self.assertEqual(build_env(True, [], "t")["CANARY_TEST_SECRET"], "s3cret")

    def test_agent_really_does_not_see_the_secret(self) -> None:
        seen = {}
        original = run_experiment

        def probe(pass_env):
            out = Path(tempfile.mkdtemp()) / "seen.txt"
            code = f"import os,pathlib; pathlib.Path({str(out)!r}).write_text('yes' if 'CANARY_TEST_SECRET' in os.environ else 'no')"
            original(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}", runs=1, channels=["visible_doc"], timeout=30, pass_env=pass_env)
            return out.read_text()

        with mock.patch.dict(os.environ, {"CANARY_TEST_SECRET": "s3cret"}):
            seen["default"] = probe([])
            seen["passed"] = probe(["CANARY_TEST_SECRET"])
        self.assertEqual(seen, {"default": "no", "passed": "yes"})

    def test_prompt_is_substituted_without_a_shell(self) -> None:
        hostile = "do it; touch PWNED $(id) `id` && echo x"
        self.assertEqual(build_command("tool --flag {prompt}", hostile), ["tool", "--flag", hostile])
        out = Path(tempfile.mkdtemp())
        code = f"import sys,pathlib; pathlib.Path({str(out / 'argv.txt')!r}).write_text(sys.argv[1]); pathlib.Path('PWNED').exists() and pathlib.Path({str(out / 'pwned')!r}).touch()"
        run_experiment(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {{prompt}}", runs=1, channels=["visible_doc"], prompt=hostile, timeout=30)
        self.assertEqual((out / "argv.txt").read_text(), hostile)
        self.assertFalse((out / "pwned").exists())

    def test_bad_arguments(self) -> None:
        with self.assertRaises(ValueError):
            run_experiment("", runs=1)
        with self.assertRaises(ValueError):
            run_experiment(agent("ignorant"), runs=0)
        with self.assertRaises(ValueError):
            run_experiment(agent("ignorant"), runs=1, mode="weird")


class TestReport(unittest.TestCase):
    def test_summary_and_warnings(self) -> None:
        results = [RunResult("a", i, False, 0, False, 0.1) for i in range(3)] + [RunResult("b", i, True, 0, False, 0.1) for i in range(3)]
        rows = {r["channel"]: r for r in summarize(results)}
        self.assertEqual(rows["a"]["followed"], 0)
        self.assertEqual(rows["b"]["rate"], 1.0)
        notes = warnings(list(rows.values()))
        self.assertEqual(len([n for n in notes if "not 'safe'" in n]), 1)
        self.assertEqual(len(notes), 1)

    def test_warns_when_runs_mostly_fail(self) -> None:
        results = [RunResult("a", i, False, 1, False, 0.1) for i in range(4)]
        notes = warnings(summarize(results))
        self.assertTrue(any("Check your --cmd" in n and "absolute paths" in n for n in notes))


class TestCLI(unittest.TestCase):
    def run_cli(self, *argv: str) -> int:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return cli.main(list(argv))

    def test_failing_exit_code_and_json_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "r.json")
            self.assertEqual(self.run_cli("run", "--cmd", agent("obedient"), "--runs", "1", "--out", out, "--quiet"), 1)
            payload = json.loads(Path(out).read_text())
            self.assertEqual(len(payload["results"]), len(CHANNELS))
            self.assertEqual(self.run_cli("report", out), 1)
            self.assertEqual(self.run_cli("report", out, "--max-rate", "1.0"), 0)

    def test_clean_agent_passes(self) -> None:
        self.assertEqual(self.run_cli("run", "--cmd", agent("ignorant"), "--runs", "1", "--quiet"), 0)

    def test_a_broken_command_is_inconclusive_not_ok(self) -> None:
        # A failing agent follows nothing, so it must never be reported as a pass.
        self.assertEqual(self.run_cli("run", "--cmd", agent("crash"), "--runs", "2", "--quiet"), 3)
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "r.json")
            self.run_cli("run", "--cmd", agent("crash"), "--runs", "2", "--out", out, "--quiet")
            self.assertEqual(self.run_cli("report", out), 3)

    def test_usage_errors_exit_2(self) -> None:
        self.assertEqual(self.run_cli("run", "--cmd", "definitely-not-a-real-binary-xyz", "--runs", "1", "--quiet"), 2)
        self.assertEqual(self.run_cli("run", "--cmd", agent("ignorant"), "--channels", "nope", "--quiet"), 2)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x").write_text("x")
            self.assertEqual(self.run_cli("init", tmp), 2)

    def test_init_writes_a_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "repo"
            self.assertEqual(self.run_cli("init", str(target), "--channels", "visible_doc", "--nonce", "deadbeef"), 0)
            self.assertIn("CANARY_visible_doc_deadbeef.txt", (target / "docs/CONTRIBUTING.md").read_text())


if __name__ == "__main__":
    unittest.main()
