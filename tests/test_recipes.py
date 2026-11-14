"""ODL-234 — the four recipes are tested, not just written.

These tests are the repo half of the card's evidence: they pin the skill's
structure, the cron templates, the watchdog state machine and the installer's
embedded bundle. The live run transcripts are under ``docs/evidence/odl-234/``.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills/odile-recipes/SKILL.md"
FIRST_RUN = ROOT / "skills/odile-first-run/SKILL.md"
WATCH = ROOT / "templates/cron/watch.py"
BRIEF = ROOT / "templates/cron/morning-brief.json"
WATCHDOG = ROOT / "templates/cron/watchdog.json"
AGENTS = ROOT / "templates/tenant-agents.md"
RECIPES_DOC = ROOT / "docs/recipes.md"
INSTALLER = ROOT / "installer/odile_install.py"
SIGNATURE = ROOT / "installer/odile_install.py.sig"
PUBKEY = ROOT / "installer/odile-signing-key.pub"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def block(text: str, start: str, end: str) -> str:
    """The text between two markers, fence removed."""
    pattern = re.compile(re.escape(start) + r"\n```[a-z]*\n(.*?)\n```\n" + re.escape(end),
                         re.DOTALL)
    match = pattern.search(text)
    if not match:
        raise AssertionError(f"markers {start!r} … {end!r} not found")
    return match.group(1)


class SkillShape(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_frontmatter_names_the_skill_and_its_four_triggers(self) -> None:
        self.assertTrue(self.text.startswith("---\nname: odile-recipes\n"))
        description = re.search(r"^description: (.+)$", self.text, re.M)
        assert description is not None
        for word in ("brief", "watchdog", "memo", "CSV"):
            self.assertIn(word, description.group(1))

    def test_four_recipes_are_headings_in_order(self) -> None:
        headings = re.findall(r"^## \d\. (.+)$", self.text, re.M)
        self.assertEqual(headings, ["Morning brief", "Silent watchdog",
                                    "Research memo", "CSV chart"])

    def test_every_recipe_has_a_trigger_a_done_line_and_a_path(self) -> None:
        sections = re.split(r"^## \d\. ", self.text, flags=re.M)[1:]
        self.assertEqual(len(sections), 4)
        for section in sections:
            self.assertIn("**When.**", section)
            self.assertIn("**Done.**", section)
            self.assertRegex(section, r"~/[a-z]+/")

    def test_the_watchdog_script_in_the_skill_is_the_tested_template(self) -> None:
        embedded = block(self.text, "<!-- BEGIN watch.py -->", "<!-- END watch.py -->")
        self.assertEqual(embedded, WATCH.read_text(encoding="utf-8").rstrip("\n"))
        self.assertIn(hashlib.sha256(WATCH.read_bytes()).hexdigest(), self.text,
                      "the skill must name the tested script's sha256")

    def test_the_brief_prompt_in_the_skill_is_the_template_prompt(self) -> None:
        template = json.loads(BRIEF.read_text(encoding="utf-8"))
        prompts = re.findall(r"^```\n(Morning brief for .*?)\n```$", self.text, re.M | re.S)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0], template["prompt"])

    def test_the_brief_prompt_matches_the_first_run_skill(self) -> None:
        """Two prompts for one job would drift; the words must be the same."""
        first = FIRST_RUN.read_text(encoding="utf-8")
        prompts = re.findall(r"^```\n(Morning brief for .*?)\n```$", first, re.M | re.S)
        self.assertEqual(len(prompts), 1)
        template = json.loads(BRIEF.read_text(encoding="utf-8"))
        normal = lambda value: " ".join(value.split())  # noqa: E731
        self.assertEqual(normal(prompts[0]), normal(template["prompt"]))

    def test_the_tenant_context_points_at_the_recipes(self) -> None:
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("odile-recipes", text)
        # A model that reads "load the skill" as advice can still improvise a
        # hand-written watchdog; the standing context must name the tool call.
        self.assertIn("skill_view(name='odile-recipes')", text)
        self.assertIn("never write your own script", text)

    def test_the_doc_names_each_recipe_and_its_landing_path(self) -> None:
        doc = RECIPES_DOC.read_text(encoding="utf-8")
        for word, path in (("Morning brief", "~/briefs/"),
                           ("watchdog", "~/workspace/watchdog/"),
                           ("memo", "~/workspace/memos/"),
                           ("CSV", "~/workspace/analysis/")):
            self.assertIn(word, doc)
            self.assertIn(path, doc)


class CronTemplates(unittest.TestCase):
    def test_morning_brief_is_a_complete_local_job(self) -> None:
        data = json.loads(BRIEF.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "Morning brief")
        self.assertRegex(data["schedule"], r"^<minute> <hour> \* \* \*$")
        self.assertEqual(data["timezone"], "<IANA zone>")
        self.assertEqual(data["deliver"], "local")
        self.assertEqual(data["enabled_toolsets"], ["web", "terminal"])
        for placeholder in ("<name>", "<zone>", "<city>", "<thing 1>", "<thing 3>"):
            self.assertIn(placeholder, data["prompt"])
        self.assertIn("Under 200 words", data["prompt"])
        self.assertIn("Write the file even if some lookups fail", data["prompt"])

    def test_watchdog_is_a_no_agent_script_job(self) -> None:
        data = json.loads(WATCHDOG.read_text(encoding="utf-8"))
        job = data["job"]
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["script"], "watch-<slug>.py")
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["schedule"], "every 6h")
        config = data["watch_config"]
        self.assertEqual(config["version"], 1)
        self.assertEqual(config["max_failures"], 3)
        self.assertTrue(config["log"].startswith("~/workspace/watchdog/"))

    def test_the_watchdog_script_has_no_third_party_imports(self) -> None:
        source = WATCH.read_text(encoding="utf-8")
        imports = set(re.findall(r"^(?:from|import) ([a-zA-Z0-9_.]+)", source, re.M))
        self.assertEqual(imports, {
            "__future__", "argparse", "hashlib", "json", "os", "re", "sys",
            "urllib.error", "urllib.request", "datetime", "pathlib",
        })


class WatchdogStateMachine(unittest.TestCase):
    """The exact promise: silent until a change, one alert per change."""

    def setUp(self) -> None:
        self.watch = load_module(WATCH, "odile_watch_template")
        self.config = {
            "version": 1,
            "url": "https://example.com/pricing",
            "label": "Example pricing",
            "match": r"Pro plan[^$]*\$([0-9.]+)",
            "group": 1,
            "max_failures": 3,
        }

    def tick(self, state, body):
        return self.watch.run_once(self.config, state, body)

    def test_baseline_is_silent_and_writes_the_state(self) -> None:
        state, alert, summary = self.tick(None, "Pro plan $49")
        self.assertIsNone(alert)
        self.assertEqual(summary, "baseline")
        self.assertEqual(state["preview"], "49")
        self.assertEqual(state["failures"], 0)

    def test_an_identical_tick_is_silent(self) -> None:
        state, _, _ = self.tick(None, "Pro plan $49")
        state2, alert, summary = self.tick(state, "Pro plan $49")
        self.assertIsNone(alert)
        self.assertEqual(summary, "unchanged")
        self.assertEqual(state2["hash"], state["hash"])

    def test_a_change_alerts_once_then_goes_silent(self) -> None:
        state, _, _ = self.tick(None, "Pro plan $49")
        state, alert, summary = self.tick(state, "Pro plan $59")
        self.assertEqual(summary, "changed")
        self.assertIn("Changed: 49 → 59", alert)
        state, alert, summary = self.tick(state, "Pro plan $59")
        self.assertIsNone(alert)
        self.assertEqual(summary, "unchanged")

    def test_failures_alert_once_at_the_threshold_then_stay_silent(self) -> None:
        state, alert, _ = self.tick(None, None)
        self.assertIsNone(alert)
        state, alert, _ = self.tick(state, None)
        self.assertIsNone(alert)
        state, alert, _ = self.tick(state, None)
        self.assertIn("3 consecutive tries", alert)
        state, alert, _ = self.tick(state, None)
        self.assertIsNone(alert)
        state, alert, summary = self.tick(state, "Pro plan $49")
        self.assertIsNone(alert)
        self.assertEqual(summary, "baseline")
        self.assertEqual(state["failures"], 0)

    def test_a_recovered_source_resets_the_failure_counter_silently(self) -> None:
        state, _, _ = self.tick(None, "Pro plan $49")
        state, _, _ = self.tick(state, None)
        state, alert, summary = self.tick(state, "Pro plan $49")
        self.assertIsNone(alert)
        self.assertEqual(summary, "unchanged")
        self.assertEqual(state["failures"], 0)
        self.assertFalse(state["alerted_failure"])

    def test_a_state_with_another_version_is_a_fresh_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(json.dumps({"version": 0, "hash": "old"}), encoding="utf-8")
            self.assertIsNone(self.watch.load_state(path))

    def test_extract_value_handles_groups_and_whole_pages(self) -> None:
        self.assertEqual(self.watch.extract_value("Pro plan $49", r"Pro plan \$([0-9.]+)", None), "49")
        self.assertEqual(self.watch.extract_value("Pro plan $49", r"Pro plan \$[0-9.]+", 0),
                         "Pro plan $49")
        with self.assertRaises(ValueError):
            self.watch.extract_value("no price", r"Pro plan \$([0-9.]+)", None)
        self.assertEqual(self.watch.extract_value("  a\n b  ", None, None), "a b")

    def test_preview_and_state_are_bounded(self) -> None:
        self.assertEqual(len(self.watch.preview("x" * 500)), self.watch.PREVIEW_CHARS)
        self.assertTrue(self.watch.preview("x" * 500).endswith("…"))

    def test_a_config_without_a_label_or_url_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.json"
            path.write_text(json.dumps({"url": "https://example.com"}), encoding="utf-8")
            with self.assertRaises(self.watch.ConfigError):
                self.watch.load_config(path)


class InstallerBundle(unittest.TestCase):
    def test_the_embedded_bundle_matches_the_sources(self) -> None:
        """A stale embed (skill edited, installer not re-embedded) fails here."""
        embed = load_module(ROOT / "installer/embed_tenant.py", "odile_embed_tenant")
        agents = embed.read_source(ROOT / "templates/tenant-agents.md")
        skills = [(name, path, embed.read_source(path)) for name, path in embed.skill_sources()]
        names = [name for name, _, _ in skills]
        self.assertIn("odile-recipes", names)
        self.assertIn("odile-first-run", names)
        expected = embed.render_block(agents, skills)
        self.assertIn(expected, INSTALLER.read_text(encoding="utf-8"))

    def test_the_installer_signature_verifies(self) -> None:
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--verify-signature", "--pubkey", str(PUBKEY)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("signature: OK", result.stdout)
        self.assertTrue(SIGNATURE.is_file())


if __name__ == "__main__":
    unittest.main()
