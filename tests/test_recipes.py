"""ODL-234 — the four recipes are tested, not just written.

These tests are the repo half of the card's evidence: they pin the skill's
structure, the cron templates, the watchdog state machine and the installer's
embedded bundle. The live run transcripts are under ``docs/evidence/odl-234/``.
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pwd
import dataclasses
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
SEND_BRIEF = ROOT / "scripts/send_brief.py"
BRIEF_TEMPLATE = ROOT / "templates/brief.txt"
SENDER_COMMAND = "sudo -n /usr/bin/python3 ~/.hermes/scripts/send_brief.py"


def brief_prompt(text: str) -> str:
    """The one fenced Morning-brief prompt a skill carries."""
    prompts = re.findall(r"^```\n(Morning brief for .*?)\n```$", text, re.M | re.S)
    assert len(prompts) == 1, f"expected one fenced prompt, found {len(prompts)}"
    return prompts[0]


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

    def test_the_brief_prompt_runs_the_installed_sender_as_its_last_step(self) -> None:
        """ODL-469: the job's last step is one command, never a second sender."""
        template = json.loads(BRIEF.read_text(encoding="utf-8"))
        copies = {
            "templates/cron/morning-brief.json": template["prompt"],
            "skills/odile-recipes/SKILL.md": brief_prompt(self.text),
            "skills/odile-first-run/SKILL.md": brief_prompt(FIRST_RUN.read_text(encoding="utf-8")),
        }
        normal = lambda value: " ".join(value.split())  # noqa: E731
        for name, prompt in copies.items():
            flat = normal(prompt)
            self.assertEqual(flat.count(SENDER_COMMAND), 1, name)
            self.assertIn("report its one line", flat, name)
            self.assertIn("Do not re-implement the sender", flat, name)
        self.assertEqual({normal(prompt) for prompt in copies.values()},
                         {normal(copies["templates/cron/morning-brief.json"])})

    def test_the_first_run_asks_for_the_address_and_writes_the_address_file(self) -> None:
        """ODL-469: the ritual asks one more thing and carries no address in the seed."""
        text = " ".join(FIRST_RUN.read_text(encoding="utf-8").split())
        self.assertIn("May I email the brief too?", text)
        self.assertIn("Which address?", text)
        self.assertIn("~/.hermes/brief.json", text)
        self.assertIn('{"to": "<address>"}', text)
        self.assertIn("chmod 600", text)

    def test_the_first_run_close_does_not_promise_an_unlock(self) -> None:
        """ODL-925 follow-up: the sealed boot has no unlock step.

        The disk unlocks on the boot the user verifies and the machine comes
        back by itself, so the close may only carry the deck's repeating-job
        row (A11): a job due while the machine was down runs when it comes back
        up. Both halves are asserted: the retired timing sentence is gone and
        the approved one is present, whitespace-flattened so a re-wrap cannot
        hide either.
        """
        source = FIRST_RUN.read_text(encoding="utf-8")
        close = source.split("## Close — exactly three sentences", 1)[1].split("\n## ", 1)[0]
        text = " ".join(line.lstrip("> ").strip() for line in close.splitlines())
        self.assertNotIn("when you unlock", text)
        self.assertNotIn("machine is locked", text)
        self.assertIn("and if it was due while the machine was down, it runs when "
                      "the machine comes back up", text)
        claims = " ".join((ROOT / "docs/CLAIMS.md").read_text(encoding="utf-8").split())
        self.assertIn("runs when the machine comes back up if it was due while it was "
                      "down", claims, "the close must quote the deck's A11 row")

    def test_the_recipes_skill_turns_email_on_with_the_same_address_file(self) -> None:
        section = self.text.split("## 1. Morning brief", 1)[1].split("## 2.", 1)[0]
        self.assertIn("~/.hermes/brief.json", section)
        self.assertIn('{"to": "<address>"}', section)
        self.assertIn(SENDER_COMMAND, section)

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
        extras = [(target, path, embed.read_source(path))
                  for target, path in embed.extra_sources()]
        names = [name for name, _, _ in skills]
        self.assertIn("odile-recipes", names)
        self.assertIn("odile-first-run", names)
        expected = embed.render_block(agents, skills, extras)
        self.assertIn(expected, INSTALLER.read_text(encoding="utf-8"))

    def test_the_installer_signature_verifies(self) -> None:
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--verify-signature", "--pubkey", str(PUBKEY)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("signature: OK", result.stdout)
        self.assertTrue(SIGNATURE.is_file())


class TenantBundle(unittest.TestCase):
    """ODL-469: the sender and the email template travel in the signed installer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.embed = load_module(ROOT / "installer/embed_tenant.py", "odile_embed_tenant_469")
        cls.installer = load_module(INSTALLER, "odile_install_469")

    def layout(self, tmp: str):
        return self.installer.Layout.scratch(
            root=Path(tmp), user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=9151, public_url="http://bundle.test",
        )

    def test_the_bundle_names_the_two_new_targets_and_their_sources(self) -> None:
        self.assertEqual(dict(self.embed.extra_sources()), {
            "scripts/send_brief.py": SEND_BRIEF,
            "brief.txt": BRIEF_TEMPLATE,
        })

    def test_the_installer_pins_places_and_modes_each_embedded_file(self) -> None:
        files = {item.target: item for item in self.installer.tenant_files()}
        self.assertIn("scripts/send_brief.py", files)
        self.assertIn("brief.txt", files)
        self.assertEqual(files["scripts/send_brief.py"].source, "scripts/send_brief.py")
        self.assertEqual(files["scripts/send_brief.py"].data(), SEND_BRIEF.read_bytes())
        self.assertEqual(files["scripts/send_brief.py"].mode, 0o755)
        self.assertEqual(files["brief.txt"].source, "templates/brief.txt")
        self.assertEqual(files["brief.txt"].data(), BRIEF_TEMPLATE.read_bytes())
        self.assertEqual(files["brief.txt"].mode, 0o644)
        with tempfile.TemporaryDirectory() as tmp:
            layout = self.layout(tmp)
            self.assertTrue(self.installer.ensure_tenant_files(layout, False))
            script = layout.hermes_home / "scripts/send_brief.py"
            brief = layout.hermes_home / "brief.txt"
            self.assertEqual(script.read_bytes(), SEND_BRIEF.read_bytes())
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
            self.assertEqual(brief.read_bytes(), BRIEF_TEMPLATE.read_bytes())
            self.assertEqual(brief.stat().st_mode & 0o777, 0o644)
            self.assertFalse(self.installer.ensure_tenant_files(layout, False))

    def test_the_embedded_skills_carry_the_same_prompt_as_the_cron_template(self) -> None:
        """The installer's copies are regenerated from the three sources; prove it."""
        template = json.loads(BRIEF.read_text(encoding="utf-8"))["prompt"]
        normal = lambda value: " ".join(value.split())  # noqa: E731
        for name, source, _digest, text in self.installer.TENANT_SKILLS:
            if name not in ("odile-first-run", "odile-recipes"):
                continue
            self.assertEqual(normal(brief_prompt(text)), normal(template), name)

    def test_an_embedded_target_outside_the_allowlist_is_refused(self) -> None:
        files = list(self.installer.tenant_files())
        body = b"#!/usr/bin/python3\n"
        files.append(self.installer.TenantFile(
            "bin/send_brief.py", "scripts/send_brief.py",
            hashlib.sha256(body).hexdigest(), body.decode()))
        with self.assertRaises(SystemExit):
            self.installer.validate_tenant_files(files)

    def test_two_copies_of_one_target_are_refused(self) -> None:
        files = list(self.installer.tenant_files())
        files.append(dataclasses.replace(
            next(item for item in files if item.target == "brief.txt")))
        with self.assertRaises(SystemExit):
            self.installer.validate_tenant_files(files)


class RelayCa(unittest.TestCase):
    """ODL-469: the tenant caches the relay's PEM where the sender reads it."""

    PEM = ("-----BEGIN CERTIFICATE-----\n"
           "MIIBhTCCASugAwIBAgIUTESTRELAYCA0000000000000000\n"
           "-----END CERTIFICATE-----\n")

    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_module(INSTALLER, "odile_install_469_ca")

    def seed(self, **relay) -> dict:
        mail = {"host": "mail.odile.fyi", "port": 587, "username": "u", "secret": "s"}
        mail.update(relay)
        return {
            "slug": "odl-ca", "instance_id": "srv-ca", "near_api_key": "k",
            "model": "m", "support_public_key": "ssh-ed25519 x",
            "backup_repo_url": "sftp:x", "mail_relay": mail, "hermes_version": "v",
        }

    def layout(self, tmp: str):
        return self.installer.Layout.scratch(
            root=Path(tmp), user=pwd.getpwuid(os.geteuid()).pw_name,
            webapp_port=9152, public_url="http://ca.test",
        )

    def test_the_ca_is_cached_at_0644_and_the_write_is_idempotent(self) -> None:
        seed = self.seed(tls_ca=self.PEM)
        with tempfile.TemporaryDirectory() as tmp:
            layout = self.layout(tmp)
            self.assertTrue(self.installer.ensure_mail_ca(layout, seed, False))
            target = layout.path("/etc/odile/mail-ca.pem")
            self.assertEqual(target.read_bytes(), self.PEM.encode())
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            self.assertFalse(self.installer.ensure_mail_ca(layout, seed, False))

    def test_a_cached_seed_without_a_ca_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = self.layout(tmp)
            self.assertFalse(self.installer.ensure_mail_ca(layout, self.seed(), False))
            self.assertFalse(layout.path("/etc/odile/mail-ca.pem").exists())

    def test_a_seed_whose_ca_is_not_a_pem_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout = self.layout(tmp)
            with self.assertRaises(SystemExit):
                self.installer.ensure_mail_ca(layout, self.seed(tls_ca="not a certificate"), False)
            self.assertFalse(layout.path("/etc/odile/mail-ca.pem").exists())


if __name__ == "__main__":
    unittest.main()
