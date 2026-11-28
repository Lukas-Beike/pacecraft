"""Keep repository-local AI instructions aligned with the source tree."""

import json
import os
import re
import string
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / ".agents" / "skills"
MODEL_INVOCABLE_STUBS = frozenset({"deliver-issue"})
SCOPED_INSTRUCTIONS = (
    "backend/AGENTS.md",
    "public/AGENTS.md",
    "tests/AGENTS.md",
    "e2e/AGENTS.md",
    ".github/AGENTS.md",
)
REQUIRED_GUIDANCE = (
    "AGENTS.md",
    "REVIEW.md",
    ".agents/skills/README.md",
    *SCOPED_INSTRUCTIONS,
)


@unittest.skipUnless(
    os.environ.get("AI_DOCS_REQUIRED") == "1" or (ROOT / "AGENTS.md").is_file(),
    "Repository AI documentation is not packaged in the runtime image",
)
class AiDocsContractTests(unittest.TestCase):
    def test_required_guidance_exists_and_scopes_are_routed(self) -> None:
        for relative in REQUIRED_GUIDANCE:
            self.assertTrue((ROOT / relative).is_file(), relative)
        instructions = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for relative in SCOPED_INSTRUCTIONS:
            self.assertIn(f"]({relative})", instructions, relative)
        self.assertIn("](.agents/skills/README.md)", instructions)
        self.assertIn("](REVIEW.md)", instructions)

    def test_expected_backend_domains_are_documented(self) -> None:
        # The domain map lives in the backend's scoped instructions; the root
        # AGENTS.md says where (#2858).
        instructions = (ROOT / "backend" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(
            "cross-cutting owners are listed in\n  [backend/AGENTS.md]",
            (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
        )
        domains = [
            path.name
            for path in (ROOT / "backend").iterdir()
            if path.is_dir() and not path.name.startswith("_")
        ]
        self.assertTrue(domains)
        for domain in domains:
            self.assertIn(f"`{domain}/`", instructions)

    def test_skill_names_are_unique_and_frontmatter_matches_directories(self) -> None:
        skill_files = sorted(SKILL_ROOT.rglob("SKILL.md"))
        self.assertTrue(skill_files)
        skills = []
        for skill_file in skill_files:
            content = skill_file.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("---\n"), skill_file)
            frontmatter = content.split("---\n", 2)
            self.assertEqual(len(frontmatter), 3, skill_file)
            self.assertRegex(frontmatter[1], r"(?m)^description:[ \t]*\S")
            match = re.search(r"^name:[ \t]*([^\r\n]+)$", frontmatter[1], re.MULTILINE)
            if match is None:
                self.fail(str(skill_file))
            skill_name = match.group(1).strip().strip('"')
            self.assertLessEqual(len(skill_name), 64, skill_file)
            self.assertRegex(skill_name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$", skill_file)
            skills.append((skill_name, skill_file))
            self.assertEqual(skill_file.parent.name, skill_name, skill_file)
        names = [name for name, _ in skills]
        self.assertEqual(len(names), len(set(names)), names)

    def test_skill_evaluation_cases_are_valid_and_targets_exist(self) -> None:
        catalogue = json.loads(
            (
                SKILL_ROOT / "pacecraft-skill-evaluation" / "evals" / "cases.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIs(type(catalogue["catalog_version"]), int)
        self.assertGreater(catalogue["catalog_version"], 0)
        cases = catalogue["cases"]
        self.assertIsInstance(cases, list)
        self.assertTrue(cases)
        ids: set[str] = set()
        coverage: dict[str, set[str]] = {}
        for case in cases:
            with self.subTest(case=case.get("id")):
                for field in ("id", "skill", "kind", "prompt", "setup"):
                    self.assertIsInstance(case[field], str)
                    self.assertTrue(case[field].strip(), field)
                self.assertRegex(case["id"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertNotIn(case["id"], ids, "Duplicate evaluation case")
                ids.add(case["id"])
                self.assertRegex(case["skill"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertTrue((SKILL_ROOT / case["skill"] / "SKILL.md").is_file())
                self.assertIn(case["kind"], ("positive", "negative", "boundary"))
                self.assertIs(type(case["activate"]), bool)
                if case["kind"] != "boundary":
                    self.assertEqual(case["activate"], case["kind"] == "positive")
                for field in ("expected", "prohibited"):
                    self.assertIsInstance(case[field], list)
                    self.assertTrue(case[field], field)
                    for assertion in case[field]:
                        self.assertIsInstance(assertion, str)
                        self.assertTrue(assertion.strip(), field)
                coverage.setdefault(case["skill"], set()).add(case["kind"])
        skills = {path.parent.name for path in SKILL_ROOT.glob("*/SKILL.md")}
        self.assertEqual(skills - set(coverage), set(), "Skills without cases")
        for skill, kinds in coverage.items():
            self.assertEqual(kinds, {"positive", "negative", "boundary"}, skill)

    def test_repository_guidance_links_resolve(self) -> None:
        markdown_files = [ROOT / relative for relative in REQUIRED_GUIDANCE]
        markdown_files.extend(SKILL_ROOT.rglob("*.md"))
        for markdown_file in markdown_files:
            content = markdown_file.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]*\]\(([^)#]+)", content):
                if target.startswith(("http://", "https://")):
                    continue
                resolved = (markdown_file.parent / target).resolve()
                self.assertTrue(
                    resolved.is_relative_to(ROOT), f"{markdown_file}: {target}"
                )
                self.assertTrue(resolved.is_file(), f"{markdown_file}: {target}")

    def test_every_skill_is_routed_by_the_index(self) -> None:
        index = (SKILL_ROOT / "README.md").read_text(encoding="utf-8")
        routes = [
            (SKILL_ROOT / target).resolve()
            for target in re.findall(r"\[[^\]]*\]\(([^)#]+/SKILL\.md)\)", index)
        ]
        expected = {path.resolve() for path in SKILL_ROOT.rglob("SKILL.md")}
        self.assertTrue(expected)
        self.assertEqual(len(routes), len(set(routes)), "Duplicate skill routes")
        self.assertEqual(set(routes), expected, "Missing or obsolete skill routes")

    def test_validation_skill_does_not_reference_removed_test_module(self) -> None:
        validation = (
            ROOT / ".agents" / "skills" / "pacecraft-validation" / "SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("tests/test_server.py", validation)
        self.assertTrue((ROOT / "tests" / "run_tests.py").is_file())

    def test_the_lane_brief_template_renders_every_placeholder(self) -> None:
        # #3926: the binding brief lived only in a session's temp folder.
        template = SKILL_ROOT / "pacecraft-orchestrator" / "lane-brief.md"
        names = set(re.findall(r"\$([A-Z_]+)", template.read_text(encoding="utf-8")))
        self.assertEqual(
            names,
            {"LANE", "ORCHESTRATOR", "MACHINE", "ORCHESTRATOR_THREAD"}
            | {"ENVIRONMENT_ID", "QUEUE", "QUEUE_RULES"},
        )
        brief = string.Template(template.read_text(encoding="utf-8")).substitute(
            {name: f"<{name.lower()}>" for name in names}
        )
        self.assertNotIn("$", brief)
        self.assertIn("thread <orchestrator_thread> with environmentId", brief)
        self.assertIn("deliver-issue", brief)

    def test_delivery_skills_require_a_ui_walk_step_for_a_visible_function(
        self,
    ) -> None:
        # #3009: the post-release walk covers every user-visible function.
        for name in ("deliver-issue", "pr"):
            with self.subTest(skill=name):
                text = (SKILL_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn("`STEPS` of `scripts/ui_walk.mjs`", text)
        self.assertIn(
            "STEPS", (ROOT / "scripts" / "ui_walk.mjs").read_text(encoding="utf-8")
        )

    def test_claude_skill_stubs_point_to_canonical_skills(self) -> None:
        stubs = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
        self.assertTrue(stubs)
        for stub in stubs:
            with self.subTest(stub=stub.parent.name):
                name = stub.parent.name
                canonical = SKILL_ROOT / name / "SKILL.md"
                self.assertTrue(canonical.is_file())
                content = stub.read_text(encoding="utf-8")
                description = re.compile(r"(?m)^description:.*$")
                self.assertIn(f"\nname: {name}\n", content)
                # Agents deliver queued issues through deliver-issue (athlete
                # decision 2026-11-11, #2274); every other stub stays user-only.
                self.assertEqual(
                    name not in MODEL_INVOCABLE_STUBS,
                    "\ndisable-model-invocation: true\n" in content,
                )
                self.assertIn(f"`.agents/skills/{name}/SKILL.md`", content)
                self.assertEqual(
                    description.findall(content),
                    description.findall(canonical.read_text(encoding="utf-8")),
                )
                self.assertLess(
                    len(content.splitlines()), 15, "Stub holds workflow text"
                )

    def test_the_model_and_effort_rule_has_one_source(self) -> None:
        # #4290: the orchestration skill states the tiers; the agent definitions, the
        # thread hook and the other files agree with it and do not restate it.
        skill = (
            SKILL_ROOT / "orchestrated-plan-implementation" / "SKILL.md"
        ).read_text(encoding="utf-8")
        table = {
            row[0]: row[2]
            for line in skill.splitlines()
            if line.startswith("| L")
            for row in [[cell.strip() for cell in line.strip("|").split("|")]]
        }
        self.assertIn(
            "Sonnet: `implementer` high, routine `reviewer` medium", table["L2"]
        )
        self.assertIn("Opus, at most medium", table["L3"])
        self.assertIn("Haiku (low)", table["L1"])
        pinned = {}
        for name in ("explorer", "implementer", "reviewer"):
            header = (ROOT / ".claude" / "agents" / f"{name}.md").read_text(
                encoding="utf-8"
            )
            pinned[name] = tuple(
                re.findall(rf"^{key}: (\w+)$", header, re.MULTILINE)[0]
                for key in ("model", "effort")
            )
        self.assertEqual(
            pinned,
            {
                "explorer": ("haiku", "low"),
                "implementer": ("sonnet", "high"),
                "reviewer": ("sonnet", "medium"),
            },
        )
        # No thread may be launched above `high`, whatever a lane's own rule says.
        rule = json.loads(
            (ROOT / "scripts/hooks/thread-model-rule.json").read_text(encoding="utf-8")
        )
        self.assertLessEqual(set(rule["allowed"].values()), {"low", "medium", "high"})
        # An Opus delegate keeps the pinned `implementer` effort unless it is passed.
        self.assertIn(
            '`effort: "medium"`', skill.split("| L3 |", 1)[1].split("\n", 1)[0]
        )
        own = SKILL_ROOT / "orchestrated-plan-implementation" / "SKILL.md"
        for path in (
            ROOT / "docs" / "development.md",
            SKILL_ROOT / "pacecraft-orchestrator" / "lane-brief.md",
            *ROOT.glob("*AGENTS.md"),
            *ROOT.glob("*/AGENTS.md"),
            *(p for p in SKILL_ROOT.glob("*/SKILL.md") if p != own),
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.relative_to(ROOT).as_posix()):
                self.assertNotRegex(
                    text, r"(Sonnet|Opus|Haiku)[\s\S]{0,20}\b(low|medium|high)\b"
                )

    def test_skills_stay_repository_local(self) -> None:
        self.assertTrue(SKILL_ROOT.is_dir(), SKILL_ROOT)
        self.assertTrue(SKILL_ROOT.resolve().is_relative_to(ROOT))
        self.assertFalse(
            list((ROOT / ".codex" / "skills").rglob("SKILL.md")),
            "Project skills have one canonical root: .agents/skills",
        )
        for skill_file in SKILL_ROOT.rglob("SKILL.md"):
            self.assertEqual(skill_file.parent.parent, SKILL_ROOT, skill_file)
            self.assertTrue(
                skill_file.resolve().is_relative_to(SKILL_ROOT.resolve()), skill_file
            )


if __name__ == "__main__":
    unittest.main()
