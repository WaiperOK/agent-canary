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
import subprocess

from agent_canary.channels import CHANNELS, NEUTRAL_FILES, canary_name, hide, load_custom_channels, write_repo
from agent_canary.report import compare, format_markdown, summarize, warnings
from agent_canary.runner import RunResult, build_command, build_env, plan, preflight_notes, run_experiment
from agent_canary.stats import fisher_exact, wilson

FAKE = Path(__file__).resolve().parents[1] / "src" / "agent_canary" / "fake_agent.py"


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
                if channel.kind == "git":
                    text = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=tmp, capture_output=True, text=True).stdout
                else:
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
            log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=tmp, capture_output=True, text=True).stdout
            self.assertNotIn("CANARY_", log)

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
        self.assertEqual(len(CHANNELS), 12)

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


class TestNewChannels(unittest.TestCase):
    def test_package_json_stays_valid_and_carries_the_canary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            canaries = write_repo(Path(tmp), ["package_json"], "abcd1234")
            data = json.loads((Path(tmp) / "package.json").read_text())
            self.assertIn(canaries["package_json"], data["description"])

    def test_failing_test_channel_shows_the_instruction_only_when_tests_run(self) -> None:
        with tempfile.TemporaryDirectory() as bad, tempfile.TemporaryDirectory() as good:
            canaries = write_repo(Path(bad), ["test_failure"], "abcd1234")
            write_repo(Path(good), ["readme_comment"], "abcd1234")
            run = lambda d: subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=d, capture_output=True, text=True)
            self.assertNotEqual(run(bad).returncode, 0)
            self.assertIn(canaries["test_failure"], run(bad).stderr)
            self.assertEqual(run(good).returncode, 0, "the neutral repository's tests must pass")

    def test_commit_message_channel_is_only_in_git_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            canaries = write_repo(Path(tmp), ["commit_message"], "abcd1234")
            for path in Path(tmp).rglob("*"):
                if path.is_file() and ".git" not in path.relative_to(tmp).parts:
                    self.assertNotIn("CANARY_", path.read_text(encoding="utf-8"), path)
            log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=tmp, capture_output=True, text=True).stdout
            self.assertIn(canaries["commit_message"], log)

    def test_obedient_agent_reads_git_history_too(self) -> None:
        results = run_experiment(agent("selective", "--only", "commit_message"), runs=1, channels=["commit_message", "vendor_doc"], timeout=30)
        self.assertEqual(rate_by_channel(results), {"commit_message": 1, "vendor_doc": 0})


class TestCustomChannels(unittest.TestCase):
    def write(self, entries) -> Path:
        path = Path(tempfile.mkdtemp()) / "channels.json"
        path.write_text(json.dumps(entries))
        return path

    def good(self, **over):
        entry = {"name": "faq_note", "path": "docs/FAQ.md", "template": "\n<!-- {instruction} -->\n"}
        entry.update(over)
        return entry

    def test_valid_custom_channel_runs_end_to_end(self) -> None:
        custom = load_custom_channels(self.write([self.good()]))
        with tempfile.TemporaryDirectory() as tmp:
            canaries = write_repo(Path(tmp), ["faq_note"], "abcd1234", custom)
            self.assertIn(canaries["faq_note"], (Path(tmp) / "docs/FAQ.md").read_text())
        results = run_experiment(agent("obedient"), runs=1, channels=["faq_note"], timeout=30, extra_channels=custom)
        self.assertEqual(rate_by_channel(results), {"faq_note": 1})

    def test_the_documented_example_file_loads(self) -> None:
        example = Path(__file__).resolve().parents[1] / "docs" / "custom-channels.example.json"
        custom = load_custom_channels(example)
        self.assertEqual(sorted(custom), ["changelog_entry", "faq_note"])
        with tempfile.TemporaryDirectory() as tmp:
            canaries = write_repo(Path(tmp), list(custom), "abcd1234", custom)
            self.assertIn(canaries["changelog_entry"], (Path(tmp) / "CHANGELOG.md").read_text())

    def test_path_traversal_and_absolute_paths_are_rejected(self) -> None:
        for bad in ("../outside.md", "docs/../../x", "/etc/cron.d/x", ".git/hooks/post-commit", "C:\\x", "docs\\..\\..\\x", ""):
            with self.assertRaises(ValueError, msg=bad):
                load_custom_channels(self.write([self.good(path=bad)]))

    def test_invalid_definitions_are_rejected(self) -> None:
        cases = [self.good(name="Bad Name"), self.good(name="visible_doc"), self.good(template="no placeholder"), self.good(template=5)]
        for entry in cases:
            with self.assertRaises(ValueError, msg=str(entry)):
                load_custom_channels(self.write([entry]))
        with self.assertRaises(ValueError):
            load_custom_channels(self.write([self.good(), self.good()]))  # duplicate name
        with self.assertRaises(ValueError):
            load_custom_channels(self.write({"not": "a list"}))

    def test_a_channel_cannot_write_outside_the_repository_even_if_loaded_directly(self) -> None:
        from agent_canary.channels import Channel
        sneaky = {"x": Channel("x", "../escape.txt", "bad", lambda base, canary: base + canary)}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            with self.assertRaises(ValueError):
                write_repo(root, ["x"], "abcd1234", sneaky)
            self.assertFalse((Path(tmp) / "escape.txt").exists())


class TestCompare(unittest.TestCase):
    def test_fisher_matches_hand_computed_values(self) -> None:
        self.assertAlmostEqual(fisher_exact(0, 5, 5, 0), 2 / 252, places=6)
        self.assertAlmostEqual(fisher_exact(2, 0, 0, 2), 1 / 3, places=6)
        self.assertEqual(fisher_exact(3, 2, 3, 2), 1.0)
        self.assertEqual(fisher_exact(5, 0, 5, 0), 1.0)
        for table in [(3, 2, 3, 2), (4, 4, 4, 4), (10, 10, 10, 10), (7, 3, 7, 3)]:
            self.assertLessEqual(fisher_exact(*table), 1.0, table)  # never above one, on any Python version
        with self.assertRaises(ValueError):
            fisher_exact(-1, 1, 1, 1)

    def test_compare_two_experiments(self) -> None:
        a = [RunResult("c1", i, False, 0, False, 0.1) for i in range(5)] + [RunResult("only_a", 0, True, 0, False, 0.1)]
        b = [RunResult("c1", i, True, 0, False, 0.1) for i in range(5)]
        rows = {r["channel"]: r for r in compare(a, b)}
        self.assertAlmostEqual(rows["c1"]["p_value"], 2 / 252, places=6)
        self.assertEqual(rows["c1"]["a"], (0, 5))
        self.assertIsNone(rows["only_a"]["p_value"])

    def test_cli_compare_reads_two_result_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = str(Path(tmp) / "a.json"), str(Path(tmp) / "b.json")
            run = lambda cmd, out, label: cli.main(["run", "--cmd", cmd, "--runs", "5", "--channels", "visible_doc", "--out", out, "--label", label, "--quiet"])
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                self.assertEqual(run(agent("ignorant"), a, "sandboxed"), 0)
                self.assertEqual(run(agent("obedient"), b, "open"), 1)
            self.assertEqual(json.loads(Path(a).read_text())["label"], "sandboxed")
            out = io_capture(lambda: cli.main(["compare", a, b]))
            self.assertIn("sandboxed", out)
            self.assertIn("0.008", out)


def io_capture(fn) -> str:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


class TestParallelAndPlanning(unittest.TestCase):
    def test_parallel_results_match_sequential_and_keep_order(self) -> None:
        channels = ["visible_doc", "hidden_unicode", "ci_comment", "vendor_doc"]
        seq = run_experiment(agent("selective", "--only", "visible_doc,ci_comment"), runs=2, channels=channels, timeout=30, jobs=1)
        par = run_experiment(agent("selective", "--only", "visible_doc,ci_comment"), runs=2, channels=channels, timeout=30, jobs=4)
        self.assertEqual([(r.channel, r.run, r.followed) for r in seq], [(r.channel, r.run, r.followed) for r in par])

    def test_invalid_jobs_and_unknown_channel(self) -> None:
        with self.assertRaises(ValueError):
            run_experiment(agent("ignorant"), runs=1, jobs=0)
        with self.assertRaises(ValueError):
            run_experiment(agent("ignorant"), runs=1, channels=["nope"])

    def test_plan_counts_runs(self) -> None:
        self.assertEqual(plan(3, ["a", "b"], "isolated")[0], 6)
        self.assertEqual(plan(3, ["a", "b"], "combined")[0], 3)

    def test_dry_run_runs_nothing(self) -> None:
        marker = Path(tempfile.mkdtemp()) / "ran.txt"
        code = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')"
        cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        with mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["run", "--cmd", cmd, "--runs", "2", "--dry-run"]), 0)
        self.assertFalse(marker.exists())


class TestPreflightAndFormats(unittest.TestCase):
    def test_preflight_flags_reachable_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".ssh").mkdir()
            notes = preflight_notes({"HOME": home, "AWS_SECRET_ACCESS_KEY": "x", "CANARY_PROMPT": "p"})
            self.assertTrue(any(".ssh" in n for n in notes))
            self.assertTrue(any("AWS_SECRET_ACCESS_KEY" in n for n in notes))
            self.assertEqual(preflight_notes({"HOME": tempfile.mkdtemp(), "PATH": "/bin", "CANARY_PROMPT": "p"}), [])

    def test_markdown_and_json_formats(self) -> None:
        results = [RunResult("a", i, i == 0, 0, False, 0.1) for i in range(4)]
        md = format_markdown(summarize(results))
        self.assertIn("| `a` | 1/4 | 25% |", md)
        with mock.patch("sys.stderr"):
            out = io_capture(lambda: cli.main(["report", self._saved(results), "--format", "json", "--max-rate", "1.0"]))
        self.assertEqual(json.loads(out)[0]["followed"], 1)

    def _saved(self, results) -> str:
        path = Path(tempfile.mkdtemp()) / "r.json"
        path.write_text(json.dumps({"version": 2, "results": [r.to_dict() for r in results]}))
        return str(path)

    def test_selftest_passes(self) -> None:
        with mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["selftest"]), 0)

    def test_results_file_records_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "r.json")
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                cli.main(["run", "--cmd", agent("ignorant"), "--runs", "1", "--channels", "visible_doc", "--out", out, "--quiet"])
            meta = json.loads(Path(out).read_text())
            for key in ("tool_version", "started_at", "python", "mode", "channels", "prompt", "cmd"):
                self.assertIn(key, meta)


if __name__ == "__main__":
    unittest.main()
