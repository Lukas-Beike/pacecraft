"""Check every repository Python file against explicit diagnostic debt."""

from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/quality_baseline.json"
# The tools read their settings from this file in the checked tree.
PYPROJECT = "pyproject.toml"
# Ruff's executable-bit rules read a file mode that Windows lacks, so Ruff skips
# them there while Linux CI reports them (#3091). On Windows the ratchet reports
# them from the git modes, with Ruff's messages, so the findings match CI's.
EXECUTABLE_RULES = {
    "EXE001": "Shebang is present but file is not executable",
    "EXE002": "The file is executable but no shebang is present",
}
GIT_EXECUTABLE = "100755"
REQUIREMENTS = ROOT / "requirements-dev.txt"
TOOLS = ("ruff", "mypy")
# The cognitive counter is this module's own code and has no pinned version, so
# it stays out of TOOLS (version recording and alignment) but is accepted debt.
COGNITIVE = "cognitive"
COGNITIVE_CODE = "S3776"
COGNITIVE_LIMIT = 15
# Sonar's python:S3358: a conditional expression nested in another (#3271).
NESTED_CONDITIONAL_CODE = "S3358"
# Sonar's python:S1192 as sonar-python's StringLiteralDuplicationCheck counts it
# (#3381): a literal of at least 5 characters, used 3 times in one file, unless it
# is word-like, a formatting pattern or a colour.
DUPLICATE_LITERAL_CODE = "S1192"
DUPLICATE_LITERAL_EXCLUSIONS = (
    re.compile(r"[_\-a-zA-Z0-9]+"),
    re.compile(r"[0-9{} .\-_%:dfrsymhYMHS<>]+"),
    re.compile(r"#[0-9a-fA-F]{6}"),
)
# Sonar's python:S8786 (super-linear regex): the patterns of these re calls go
# through Sonar's own engine, sonarjs/super-linear-regex (#3381).
SLOW_REGEX_RULE = "sonarjs/super-linear-regex"
# python:S5843 (regex too complex, threshold 20) failed #3781 only in CI
# (#3792); sonarjs/regex-complexity is the same check.
REGEX_RULES = {
    SLOW_REGEX_RULE: "Super-linear regular expression",
    "sonarjs/regex-complexity": "Too complex regular expression",
}
REGEX_CALLS = frozenset(
    {"compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall"}
    | {"finditer"}
)
# Loads sonarjs from the same node_modules as ESLint (PACECRAFT_NODE_MODULES).
REGEX_ESLINT_CONFIG = """import { createRequire } from "node:module";
const load = createRequire(__MODULES__ + "/_");
export default [{ files: ["*.mjs"], plugins: { sonarjs: load("eslint-plugin-sonarjs") },
  languageOptions: { ecmaVersion: "latest", sourceType: "module" },
  rules: { "sonarjs/super-linear-regex": "error", "sonarjs/regex-complexity": "error" } }];
"""
# Sonar's css:S4666 (Stylelint no-duplicate-selectors): the same selector list in
# the same at-rule context twice. Like the cognitive counter it is this module's
# own code; CI's Sonar gate failed on it while every local check passed (#3077).
CSS_DUPLICATE = "css"
CSS_DUPLICATE_CODE = "S4666"
# Inside these at-rules a "selector" is a keyframe step or a descriptor block.
CSS_NO_SELECTORS = ("@keyframes", "@-webkit-keyframes", "@font-face", "@page")
# A Python file `ruff format` would rewrite (#3061). The files unformatted when
# the check arrived are baseline debt; one a push leaves unformatted is new.
FORMAT = "format"
# Instruction files may not grow (#3925): their line counts are recorded too.
INSTRUCTION_GLOBS = ("AGENTS.md", "*/AGENTS.md", ".agents/skills/*/SKILL.md")
# Line and column are reported for humans but are not debt identity, so an
# unrelated edit that moves a known diagnostic does not fail the ratchet.
IDENTITY_FIELDS = ("tool", "path", "code", "message_sha256")
NON_SOURCE_DIRECTORIES = {
    ".git",
    ".graphify-local",
    ".quality-tools",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "data",
    ".mypy_cache",
    ".ruff_cache",
}
# Windows limits a command line to 32767 characters, which the repository's
# file list exceeds: Ruff checks files independently, so it runs in batches
# within this path budget, while mypy reads the complete list from an argument
# file to keep cross-module analysis.
COMMAND_LINE_PATH_BUDGET = 30000
# Sonar's JavaScript rules S3776 (cognitive complexity) and S3358 (nested
# ternary) through eslint-plugin-sonarjs. CI's quality job installs no Node
# packages, so there the step reports that it was skipped; the pre-push hook
# points PACECRAFT_NODE_MODULES at the clone because its HEAD export has none,
# and there a missing ESLint (node_modules older than the pin) is fatal.
ESLINT_CONFIG = ROOT / "tools/eslint.sonar.config.mjs"
# Every JavaScript file SonarCloud analyzes (#3058): the page scripts, and the
# Node scripts, browser specs and unit tests.
JAVASCRIPT_GLOBS = (
    "public/*.js",
    "scripts/**/*.mjs",
    "tools/**/*.mjs",
    "e2e/**/*.js",
    "tests/**/*.cjs",
    "*.cjs",
)


# scripts/prepare_worktree.py installs Ruff and mypy into the worktree's own
# directory (#3217); the pre-push hook runs this script in an export of HEAD and
# names the clone in PACECRAFT_GIT_ROOT.
TOOLS_DIR = Path(os.environ.get("PACECRAFT_GIT_ROOT") or ROOT) / ".quality-tools"


def own_tool(tool: str) -> Path | None:
    """The worktree's prepared tool, or None: never in CI, where a committed
    `.quality-tools` must not replace the interpreter's tools, and never half
    installed."""
    entry = TOOLS_DIR / tool / "__main__.py"
    if tool == "ruff":
        entry = TOOLS_DIR / "bin" / ("ruff.exe" if os.name == "nt" else "ruff")
    return None if os.environ.get("CI") or not entry.is_file() else entry


def tool_command(tool: str) -> list[str]:
    """Run the worktree's own tool when prepared, else this interpreter's (CI).

    Ruff runs its binary from there directly: `python -m ruff` would start the
    interpreter's own binary first.
    """
    if not own_tool(tool):
        return [sys.executable, "-m", tool]
    if tool == "ruff":
        return [str(own_tool(tool))]
    loader = (
        f"import runpy, sys; sys.path.insert(0, {str(TOOLS_DIR)!r}); "
        f"runpy.run_module({tool!r}, run_name='__main__', alter_sys=True)"
    )
    return [sys.executable, "-c", loader]


def path_batches(files: list[str]) -> list[list[str]]:
    batches: list[list[str]] = [[]]
    size = 0
    for path in files:
        # A separator and possible quotes accompany each path.
        length = len(path) + 3
        if batches[-1] and size + length > COMMAND_LINE_PATH_BUDGET:
            batches.append([])
            size = 0
        batches[-1].append(path)
        size += length
    return batches


def source_files(root: Path) -> list[str]:
    files: list[str] = []
    for directory, children, filenames in os.walk(root):
        children[:] = [
            name
            for name in children
            if name != "__pycache__"
            and not (Path(directory) == root and name in NON_SOURCE_DIRECTORIES)
        ]
        files.extend(
            (Path(directory) / name).relative_to(root).as_posix()
            for name in filenames
            if name.endswith((".py", ".pyi"))
        )
    return sorted(files)


def tool_name(command: list[str]) -> str:
    if command[1:2] == ["-m"]:
        return command[2]
    loaded = (
        re.search(r"run_module\('(\w+)'", command[2])
        if command[1:2] == ["-c"]
        else None
    )
    return loaded.group(1) if loaded else Path(command[0]).stem


def run(command: list[str], root: Path, allowed: tuple[int, ...] = (0, 1)) -> str:
    # Decode as UTF-8 and make Python tools (mypy) write UTF-8 too; the Windows
    # code page left stdout None on a finding it cannot decode (#2889).
    result = subprocess.run(
        command,
        cwd=root,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode not in allowed:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise RuntimeError(
            f"{tool_name(command)} failed with exit {result.returncode}{suffix}"
        )
    if result.returncode == 1 and not result.stdout.strip():
        raise RuntimeError(f"{tool_name(command)} failed without diagnostic output")
    return result.stdout


def diagnostic(tool: str, item: dict, root: Path) -> dict:
    path = Path(item["filename"] if tool == "ruff" else item["file"])
    if not path.is_absolute():
        path = root / path
    location = item["location"] if tool == "ruff" else item
    return {
        "tool": tool,
        "path": path.resolve().relative_to(root.resolve()).as_posix(),
        "line": location["row"] if tool == "ruff" else location["line"],
        "column": location["column"],
        "code": item["code"],
        "message_sha256": hashlib.sha256(item["message"].encode()).hexdigest(),
    }


# Structures that count 1 plus nesting; their listed fields sit at their own
# level and everything else (the body, handlers, cases) one level deeper.
_COGNITIVE_HEADS = {
    ast.For: ("target", "iter"),
    ast.AsyncFor: ("target", "iter"),
    ast.While: ("test",),
    ast.ExceptHandler: ("type",),
    ast.Match: ("subject",),
}


def _cognitive_score(node: ast.AST, nesting: int, operator: type | None = None) -> int:
    """Score one node and its descendants; `operator` is a boolean parent's kind."""
    if isinstance(node, ast.BoolOp):
        # Like operators in one chain, even across parentheses, count once.
        own = 0 if operator is type(node.op) else 1
        return own + sum(
            _cognitive_score(value, nesting, type(node.op)) for value in node.values
        )
    if isinstance(node, ast.If):
        return _if_score(node, nesting, 1 + nesting)
    heads = _COGNITIVE_HEADS.get(type(node))
    if heads is not None:
        return (
            1
            + nesting
            + sum(
                _cognitive_score(child, nesting + (field not in heads))
                for field, value in ast.iter_fields(node)
                for child in (value if isinstance(value, list) else [value])
                if isinstance(child, ast.AST)
            )
        )
    own = 1 + nesting if isinstance(node, ast.IfExp) else 0
    if isinstance(
        node, ast.IfExp | ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef
    ):
        nesting += 1
    return own + sum(
        _cognitive_score(child, nesting) for child in ast.iter_child_nodes(node)
    )


def _if_score(node: ast.If, nesting: int, increment: int) -> int:
    total = increment + _cognitive_score(node.test, nesting)
    total += sum(_cognitive_score(item, nesting + 1) for item in node.body)
    rest = node.orelse
    # `elif` is an If whose keyword starts at the column of its parent's `if`.
    if (
        len(rest) == 1
        and isinstance(rest[0], ast.If)
        and rest[0].col_offset == node.col_offset
    ):
        return total + _if_score(rest[0], nesting, 1)
    if rest:
        total += 1 + sum(_cognitive_score(item, nesting + 1) for item in rest)
    return total


def cognitive_complexity(function: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return SonarQube's python:S3776 cognitive complexity of a function body.

    Structures count 1 plus their nesting level: if, ternary, for, while, except
    and match. `elif` and `else` count 1 without nesting; each chain of like
    `and`/`or` operators counts 1; lambdas and nested functions raise the nesting
    of their content, which belongs to the enclosing function. Simplifications:
    recursion, comprehensions, `else` of loops and try, and `try`/`with` blocks
    count nothing, and the decorators and defaults of the measured function are
    not scored.
    """
    return sum(_cognitive_score(statement, 0) for statement in function.body)


def _functions(node: ast.AST, prefix: str = ""):
    """Yield (qualified name, function) for every outermost function and method."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            yield prefix + child.name, child
        elif isinstance(child, ast.ClassDef):
            yield from _functions(child, f"{prefix}{child.name}.")
        else:
            yield from _functions(child, prefix)


def git_modes(root: Path) -> dict[str, str] | None:
    """Each tracked path's git mode; None without a repository.

    The pre-push hook checks an export of HEAD without `.git` and names the
    clone in PACECRAFT_GIT_ROOT, so HEAD's tree holds the modes; a working tree
    reads its index, where a staged mode change already shows.
    """
    clone = os.environ.get("PACECRAFT_GIT_ROOT")
    command = (
        ["git", "-C", clone, "ls-tree", "-r", "-z", "HEAD"]
        if clone
        else ["git", "-C", str(root), "ls-files", "-s", "-z"]
    )
    try:
        output = run(command, root, (0,))
    except RuntimeError:
        return None
    modes = {}
    # NUL-separated records keep a path unquoted, special characters included.
    for record in filter(None, output.split("\0")):
        head, _, path = record.partition("\t")
        modes[path] = head.split(" ", 1)[0]
    return modes


def executable_bit_diagnostics(
    root: Path, files: list[str], *, windows: bool = os.name == "nt"
) -> list[dict]:
    """EXE001/EXE002 as Ruff reports them on Linux; Ruff itself does elsewhere."""
    modes = git_modes(root) if windows else None
    if modes is None:
        return []
    diagnostics = []
    for path in files:
        with (root / path).open("rb") as source:
            shebang = source.read(2) == b"#!"
        executable = modes.get(path) == GIT_EXECUTABLE
        code = {(True, False): "EXE001", (False, True): "EXE002"}.get(
            (shebang, executable)
        )
        if code:
            item = {
                "filename": path,
                "location": {"row": 1, "column": 1},
                "code": code,
                "message": EXECUTABLE_RULES[code],
            }
            diagnostics.append(diagnostic("ruff", item, root))
    return diagnostics


def format_diagnostics(root: Path, files: list[str]) -> list[dict]:
    """One finding per file `ruff format --check` would rewrite."""
    paths: set[str] = set()
    for batch in path_batches(files):
        output = run(
            [
                *tool_command("ruff"),
                "format",
                "--check",
                "--no-cache",
                "--config",
                PYPROJECT,
                "--output-format=json",
                *batch,
            ],
            root,
        )
        paths.update(
            Path(item["filename"]).resolve().relative_to(root.resolve()).as_posix()
            for item in json.loads(output or "[]")
        )
    message = hashlib.sha256(b"would reformat").hexdigest()
    return [
        {"tool": FORMAT, "path": path, "code": FORMAT, "message_sha256": message}
        for path in sorted(paths)
    ]


def cognitive_diagnostics(root: Path, files: list[str]) -> list[dict]:
    """Report each function above the limit by name, never by score, so the
    baseline identity survives the score changing."""
    diagnostics = []
    for path in files:
        source = (root / path).read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, path)
        except SyntaxError as error:
            raise RuntimeError(f"Cannot parse {path} for complexity") from error
        diagnostics.extend(nested_conditional_diagnostics(tree, path))
        diagnostics.extend(duplicated_literal_diagnostics(tree, path, source))
        for name, function in _functions(tree):
            if cognitive_complexity(function) > COGNITIVE_LIMIT:
                message = f"Cognitive complexity of {name} is above {COGNITIVE_LIMIT}"
                diagnostics.append(
                    {
                        "tool": COGNITIVE,
                        "path": path,
                        "line": function.lineno,
                        "column": function.col_offset + 1,
                        "code": COGNITIVE_CODE,
                        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                    }
                )
    return diagnostics


def _inner_conditional(node: ast.AST) -> bool:
    """A conditional expression below `node`, not counting a lambda's own body."""
    return any(
        isinstance(child, ast.IfExp)
        or (not isinstance(child, ast.Lambda) and _inner_conditional(child))
        for child in ast.iter_child_nodes(node)
    )


def nested_conditional_diagnostics(tree: ast.AST, path: str) -> list[dict]:
    """One finding per conditional expression that holds another (Sonar S3358)."""
    message = "Conditional expression nested in another"
    return [
        {
            "tool": COGNITIVE,
            "path": path,
            "line": node.lineno,
            "column": node.col_offset + 1,
            "code": NESTED_CONDITIONAL_CODE,
            "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        }
        for node in ast.walk(tree)
        if isinstance(node, ast.IfExp) and _inner_conditional(node)
    ]


def _annotations(node: ast.AST) -> list[ast.AST]:
    """The type annotations one node declares."""
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    arguments = [
        arg.annotation
        for arg in ast.walk(node.args)
        if isinstance(arg, ast.arg) and arg.annotation
    ]
    return [*arguments, *([node.returns] if node.returns else [])]


def _docstring(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    """The scope's docstring: a string literal as its first statement, nothing
    else (a first call with string arguments is ordinary code, Gitar on #3388)."""
    first = node.body[0] if node.body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return [first.value] if isinstance(first.value.value, str) else []
    return []


def _uncounted_literals(tree: ast.AST) -> set[int]:
    """Ids of the string constants Sonar's S1192 skips: docstrings, f-string parts,
    and everything in a decorator or a type annotation."""
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    skipped: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, scopes):
            skipped.extend(_docstring(node))
            skipped.extend(getattr(node, "decorator_list", []))
        skipped.extend(_annotations(node))
        if isinstance(node, ast.JoinedStr):
            skipped.append(node)
    return {id(inner) for outer in skipped for inner in ast.walk(outer)}


def _literal_text(node: ast.Constant, source: str, starts: list[int]) -> str:
    """The literal as written, quotes and prefix included, as Sonar groups it."""
    end_line = node.end_lineno or node.lineno
    return source[
        starts[node.lineno - 1] + node.col_offset : starts[end_line - 1]
        + (node.end_col_offset or 0)
    ]


def duplicated_literal_diagnostics(tree: ast.AST, path: str, source: str) -> list[dict]:
    """Sonar's python:S1192 (#3381): one finding per literal used 3 times, at its
    first use, counted with its quotes, never in a file named test*."""
    if Path(path).name.startswith("test"):
        return []
    skipped = _uncounted_literals(tree)
    lines = source.splitlines(keepends=True)
    starts = [0, *itertools.accumulate(len(line) for line in lines)]
    uses: dict[str, list[ast.Constant]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if id(node) in skipped or len(value) < 5:
            continue
        if any(pattern.fullmatch(value) for pattern in DUPLICATE_LITERAL_EXCLUSIONS):
            continue
        uses.setdefault(_literal_text(node, source, starts), []).append(node)
    diagnostics = []
    for text, nodes in uses.items():
        first = min(nodes, key=lambda node: (node.lineno, node.col_offset))
        # Sonar suppresses an issue on a line marked NOSONAR (Gitar on #3388).
        if len(nodes) < 3 or "NOSONAR" in lines[first.lineno - 1]:
            continue
        message = f"Duplicated literal {text}"
        diagnostics.append(
            {
                "tool": COGNITIVE,
                "path": path,
                "line": first.lineno,
                "column": first.col_offset + 1,
                "code": DUPLICATE_LITERAL_CODE,
                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            }
        )
    return diagnostics


def _regex_patterns(tree: ast.AST) -> list[tuple[int, str, range]]:
    """(call line, pattern, the pattern's own lines) of each literal pattern an
    re call compiles; a verbose one
    (re.X) is left out, as its whitespace and comments are no pattern syntax."""
    found = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in REGEX_CALLS
        ):
            continue
        given = [
            *node.args[:1],
            *(k.value for k in node.keywords if k.arg == "pattern"),
        ]
        verbose = any(
            isinstance(inner, ast.Attribute) and inner.attr in ("X", "VERBOSE")
            for inner in ast.walk(node)
        )
        literal = given[0] if given else None
        if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
            # Sonar reports on the line where the literal starts (Gitar, #3810).
            lines = range(literal.lineno, literal.lineno + 1)
            found.extend([] if verbose else [(node.lineno, literal.value, lines)])
    return found


def _javascript_pattern(pattern: str) -> str:
    """The pattern in JavaScript syntax: named groups, their back references and
    the string anchors; a leading inline flag group carries no structure."""
    text = re.sub(r"^\(\?[aiLmsux]+\)", "", pattern)
    text = text.replace("(?P<", "(?<")
    text = re.sub(r"\(\?P=(\w+)\)", r"\\k<\1>", text)
    return text.replace("\\A", "^").replace("\\Z", "$")


def python_regex_diagnostics(root: Path, files: list[str]) -> list[dict] | None:
    """Sonar's python:S8786 and python:S5843 through sonarjs/super-linear-regex
    and sonarjs/regex-complexity; None if ESLint is not installed. A pattern
    JavaScript cannot read is skipped by the rules."""
    script = eslint_script()
    if script is None:
        return None
    patterns: list[tuple[str, int, str]] = []
    for path in files:
        source = (root / path).read_text(encoding="utf-8")
        lines = source.splitlines()
        # Sonar suppresses an issue on a line marked NOSONAR (Gitar on #3388),
        # on the call line or a line of the pattern itself (#3792).
        patterns.extend(
            (path, line, text)
            for line, text, own in _regex_patterns(ast.parse(source, path))
            if not any("NOSONAR" in lines[n - 1] for n in {line, *own})
        )
    if not patterns:
        return []
    modules = os.environ.get("PACECRAFT_NODE_MODULES") or str(ROOT / "node_modules")
    config = REGEX_ESLINT_CONFIG.replace("__MODULES__", json.dumps(modules))
    with tempfile.TemporaryDirectory(prefix="quality-regex-") as directory:
        folder = Path(directory)
        (folder / "eslint.config.mjs").write_text(config, encoding="utf-8")
        (folder / "patterns.mjs").write_text(
            "".join(
                f"new RegExp({json.dumps(_javascript_pattern(text))});\n"
                for _path, _line, text in patterns
            ),
            encoding="utf-8",
        )
        output = run(
            ["node", str(script), "--config", "eslint.config.mjs", "--format", "json"]
            + ["patterns.mjs"],
            folder,
        )
    diagnostics = []
    for item in json.loads(output)[0]["messages"]:
        path, line, text = patterns[item["line"] - 1]
        code = item.get("ruleId")
        if code not in REGEX_RULES:
            # A parse or plugin error is no finding; it must not become debt.
            raise RuntimeError(f"ESLint regex check failed: {code or 'no rule'}")
        message = f"{REGEX_RULES[code]} {text}"
        # ESLint's finding, so the step is skipped like ESLint where Node is not
        # installed (CI's quality job), and so are its baseline entries.
        diagnostics.append(
            {
                "tool": "eslint",
                "path": path,
                "line": line,
                "column": 1,
                "code": code,
                "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
            }
        )
    return diagnostics


def _css_selector_key(prelude: str) -> str:
    """A selector list as Stylelint compares it: whitespace and order ignored."""
    for combinator in ">+~":
        prelude = prelude.replace(combinator, f" {combinator} ")
    selectors = (" ".join(part.split()) for part in prelude.split(","))
    return ", ".join(sorted(selector for selector in selectors if selector))


def _css_rule_key(
    scope: list[tuple[int, str]], prelude: str
) -> tuple[tuple[int, ...], str] | None:
    """The comparison key of a rule's selector list; None for an at-rule or a
    block inside @keyframes and similar, which hold no selectors.

    The context is the enclosing blocks themselves, not their text: two
    separate @media blocks with the same query are different parents, as in
    Stylelint.
    """
    prelude = " ".join(prelude.split())
    if prelude.startswith("@") or any(
        outer.startswith(CSS_NO_SELECTORS) for _, outer in scope
    ):
        return None
    return tuple(block for block, _ in scope), _css_selector_key(prelude)


def css_duplicate_selectors(text: str) -> list[tuple[str, int]]:
    """Each repeated selector list in the same at-rule or rule context, with the
    line of its later rule; comments are blanked so lines stay true."""
    text = re.sub(
        r"/\*.*?\*/",
        lambda match: re.sub(r"[^\n]", " ", match.group()),
        text,
        flags=re.DOTALL,
    )
    seen: set[tuple[tuple[int, ...], str]] = set()
    found: list[tuple[str, int]] = []
    scope: list[tuple[int, str]] = []
    start = 0
    for block, match in enumerate(re.finditer(r"[{};]", text)):
        if match.group() == "{":
            prelude = text[start : match.start()]
            key = _css_rule_key(scope, prelude)
            if key in seen and key is not None:
                first = start + len(prelude) - len(prelude.lstrip())
                found.append((key[1], text.count("\n", 0, first) + 1))
            if key is not None:
                seen.add(key)
            scope.append((block, " ".join(prelude.split())))
        elif match.group() == "}" and scope:
            scope.pop()
        start = match.end()
    return found


def css_duplicate_diagnostics(root: Path) -> list[dict]:
    """Sonar's css:S4666 for public/*.css, one finding per repeated rule."""
    diagnostics = []
    for css in sorted((root / "public").glob("*.css")):
        path = css.relative_to(root).as_posix()
        for selector, line in css_duplicate_selectors(css.read_text(encoding="utf-8")):
            message = f"Duplicate selector {selector}"
            diagnostics.append(
                {
                    "tool": CSS_DUPLICATE,
                    "path": path,
                    "line": line,
                    "column": 1,
                    "code": CSS_DUPLICATE_CODE,
                    "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                }
            )
    return diagnostics


def eslint_script() -> Path | None:
    modules = os.environ.get("PACECRAFT_NODE_MODULES")
    script = Path(modules or ROOT / "node_modules") / "eslint/bin/eslint.js"
    return script if script.is_file() and shutil.which("node") else None


def javascript_diagnostics(root: Path) -> list[dict] | None:
    """Sonar's complexity and nested-ternary rules for JavaScript; None if skipped."""
    script = eslint_script()
    if script is None:
        if os.environ.get("PACECRAFT_NODE_MODULES"):
            raise RuntimeError(
                "ESLint is not installed in PACECRAFT_NODE_MODULES; run `npm ci`"
            )
        print("Quality ratchet: ESLint not installed (npm ci); skipped JavaScript")
        return None
    files = sorted(
        {
            path.relative_to(root).as_posix()
            for pattern in JAVASCRIPT_GLOBS
            for path in root.glob(pattern)
            if "node_modules" not in path.parts
        }
    )
    if not files:
        return []
    output = run(
        [
            "node",
            str(script),
            "--no-config-lookup",
            "--config",
            str(ESLINT_CONFIG),
            "--format",
            "json",
            *files,
        ],
        root,
    )
    diagnostics = []
    for result in json.loads(output):
        path = Path(result["filePath"]).resolve().relative_to(root.resolve()).as_posix()
        lines = (root / path).read_text(encoding="utf-8").splitlines()
        for item in result["messages"]:
            if not item.get("ruleId"):
                raise RuntimeError(f"ESLint could not check {path}: {item['message']}")
            # Sonar suppresses an issue on a line marked NOSONAR.
            if "NOSONAR" in lines[item["line"] - 1]:
                continue
            # The score changes with the code and is not part of the identity.
            message = re.sub(r"\b\d+\b", "N", item["message"])
            diagnostics.append(
                {
                    "tool": "eslint",
                    "path": path,
                    "line": item["line"],
                    "column": item["column"],
                    "code": item["ruleId"],
                    "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
                }
            )
    return diagnostics


def collect(root: Path, notes: dict[str, str]) -> tuple[dict[str, str], list[dict]]:
    """Versions and diagnostics; `notes` receives mypy's message per finding.

    The records stay message-free (hashed), so the baseline never holds source
    text; the notes only print beside a NEW finding (#2944).
    """
    files = source_files(root)
    if not files:
        raise RuntimeError("No Python source files discovered")
    versions = {}
    if not all(own_tool(tool) for tool in TOOLS):
        print(
            "Quality tools: this interpreter's; `python scripts/prepare_worktree.py` "
            "installs the worktree's own (#3217)",
            file=sys.stderr,
        )
    for tool in TOOLS:
        output = run([*tool_command(tool), "--version"], root, (0,))
        match = re.search(r"\b\d+\.\d+\.\d+\b", output)
        if not match:
            raise RuntimeError(f"Cannot identify {tool} version")
        versions[tool] = match.group()

    diagnostics: list[dict] = []
    for batch in path_batches(files):
        ruff_output = run(
            [
                *tool_command("ruff"),
                "check",
                "--no-cache",
                "--config",
                PYPROJECT,
                "--output-format=json",
                *batch,
            ],
            root,
        )
        diagnostics.extend(
            diagnostic("ruff", item, root) for item in json.loads(ruff_output)
        )
    with tempfile.TemporaryDirectory(prefix="quality-mypy-") as cache:
        file_list = Path(cache) / "files.txt"
        file_list.write_text("\n".join(files) + "\n", encoding="utf-8")
        mypy_output = run(
            [
                *tool_command("mypy"),
                "--config-file",
                PYPROJECT,
                "--no-incremental",
                "--cache-dir",
                cache,
                "--no-error-summary",
                "--output=json",
                f"@{file_list}",
            ],
            root,
        )
    for line in mypy_output.splitlines():
        item = json.loads(line)
        if item["severity"] == "error":
            record = diagnostic("mypy", item, root)
            diagnostics.append(record)
            notes[note_key(record)] = f": {item['message']}"
    diagnostics.extend(executable_bit_diagnostics(root, files))
    diagnostics.extend(format_diagnostics(root, files))
    diagnostics.extend(cognitive_diagnostics(root, files))
    diagnostics.extend(python_regex_diagnostics(root, files) or [])
    diagnostics.extend(css_duplicate_diagnostics(root))
    diagnostics.extend(javascript_diagnostics(root) or [])

    return versions, sorted(
        diagnostics, key=lambda item: json.dumps(item, sort_keys=True)
    )


def instruction_lines(root: Path) -> dict[str, int]:
    """Line count per instruction file (AGENTS.md and skill files)."""
    return {
        path.relative_to(root).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for pattern in INSTRUCTION_GLOBS
        for path in sorted(root.glob(pattern))
    }


def size_findings(
    recorded: Mapping[str, int], sizes: Mapping[str, int]
) -> tuple[list[str], list[str]]:
    """Grown or unrecorded instruction files, and shrunk or removed ones."""
    grown = [
        f"size {path}: {recorded.get(path, 0)} -> {lines} lines"
        for path, lines in sorted(sizes.items())
        if lines > recorded.get(path, 0)
    ]
    shrunk = [
        f"size {path}: {lines} -> {sizes.get(path, 0)} lines"
        for path, lines in sorted(recorded.items())
        if sizes.get(path, 0) < lines
    ]
    return grown, shrunk


def identity(item: dict) -> dict:
    return {field: item[field] for field in IDENTITY_FIELDS}


def identity_key(item: dict) -> str:
    return json.dumps(identity(item), sort_keys=True)


def compare(expected: list[dict], actual: list[dict]) -> tuple[list[dict], list[dict]]:
    remaining = Counter(identity_key(item) for item in expected)
    added: list[dict] = []
    for item in actual:
        key = identity_key(item)
        if remaining[key]:
            remaining[key] -= 1
        else:
            added.append(item)
    obsolete = [
        json.loads(key) for key, count in remaining.items() for _ in range(count)
    ]
    return added, obsolete


def pinned_versions(requirements: str) -> dict[str, str]:
    versions = {}
    for tool in TOOLS:
        match = re.search(
            rf"^{tool}==(\d+\.\d+\.\d+)(?![\w.])", requirements, flags=re.MULTILINE
        )
        if not match:
            raise RuntimeError(f"Cannot identify the pinned {tool} version")
        versions[tool] = match.group(1)
    return versions


def aligned_baseline(baseline_text: str, requirements: str) -> str | None:
    """Return the baseline with pinned tool versions, or None when they already match.

    Only the version record changes; CI still proves the diagnostics under the
    new tools, so a bump that adds findings keeps failing for a person to fix.
    """
    baseline = json.loads(baseline_text)
    if baseline.get("schema") != 1 or set(baseline.get("versions", {})) != set(TOOLS):
        raise RuntimeError("Quality baseline schema or tools are not supported")
    versions = pinned_versions(requirements)
    if baseline["versions"] == versions:
        return None
    baseline["versions"] = versions
    return json.dumps(baseline, indent=2) + "\n"


# The skills `openspec update` generates; their size follows the pinned CLI (#4107).
OPENSPEC_SKILL = re.compile(r"\.agents/skills/openspec-[a-z0-9-]+/SKILL\.md")


def openspec_sized_baseline(baseline_text: str, sizes: dict[str, int]) -> str | None:
    """Return the baseline with the generated OpenSpec skills' line counts, or None
    when they already match. No other instruction file's entry changes."""
    baseline = json.loads(baseline_text)
    recorded = baseline.get("instruction_lines")
    if baseline.get("schema") != 1 or not isinstance(recorded, dict):
        raise RuntimeError(
            "Quality baseline schema or instruction sizes are not supported"
        )
    generated = {
        path: lines for path, lines in sizes.items() if OPENSPEC_SKILL.fullmatch(path)
    }
    stale = {path for path in recorded if OPENSPEC_SKILL.fullmatch(path)} - set(
        generated
    )
    if (
        all(recorded.get(path) == lines for path, lines in generated.items())
        and not stale
    ):
        return None
    for path in stale:
        del recorded[path]
    recorded.update(generated)
    return json.dumps(baseline, indent=2) + "\n"


def finding(item: dict) -> str:
    location = f"{item['path']}:{item['line']}" if "line" in item else item["path"]
    return f"{item['tool']} {location} {item['code']}"


def note_key(item: dict) -> str:
    """One finding's message key: its message hash, as two errors of a code
    can share a line and even a column."""
    return f"{finding(item)} {item['message_sha256']}"


def check(
    baseline: dict,
    versions: dict[str, str],
    diagnostics: list[dict],
    skipped: tuple[str, ...] = (),
    notes: Mapping[str, str] = MappingProxyType({}),
    sizes: Mapping[str, int] | None = None,
) -> int:
    if baseline["schema"] != 1:
        raise RuntimeError("Quality baseline schema does not match")
    if baseline["versions"] != versions:
        raise RuntimeError(
            f"Quality baseline tool versions {baseline['versions']} do not match "
            f"installed {versions}. Run `python tools/quality_baseline.py "
            "--align-versions` and commit tests/fixtures/quality_baseline.json."
        )
    if any(
        item.get("tool") not in {*TOOLS, COGNITIVE, FORMAT, CSS_DUPLICATE, "eslint"}
        for item in [*baseline["diagnostics"], *diagnostics]
    ):
        raise RuntimeError(
            "Quality baseline supports Ruff, Ruff format, mypy, cognitive "
            "complexity, CSS duplicate selector and ESLint diagnostics only"
        )
    expected = [item for item in baseline["diagnostics"] if item["tool"] not in skipped]
    added, obsolete = compare(expected, diagnostics)
    for label, items in (("NEW", added), ("OBSOLETE", obsolete)):
        for item in items:
            print(f"{label} {finding(item)}{notes.get(note_key(item), '')}")
    grown, shrunk = (
        ([], [])
        if sizes is None
        else size_findings(baseline.get("instruction_lines", {}), sizes)
    )
    for line in [*(f"NEW {g}" for g in grown), *(f"OBSOLETE {s}" for s in shrunk)]:
        print(line)
    if grown:
        print(
            "An instruction file may not grow: remove as many lines as you add "
            "(#3925); a reviewed exception is recorded with --snapshot."
        )
    added_count = len(added) + len(grown)
    obsolete_count = len(obsolete) + len(shrunk)
    print(
        f"Quality ratchet: {len(diagnostics)} existing, {added_count} new, "
        f"{obsolete_count} obsolete"
    )
    if obsolete or shrunk:
        print(
            "Obsolete baseline entries must be removed. Regenerate with "
            "`python tools/quality_baseline.py --snapshot --base <commit>`, which "
            "rewrites tests/fixtures/quality_baseline.json."
        )
    return 1 if added_count or obsolete_count else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--base")
    parser.add_argument(
        "--align-versions",
        action="store_true",
        help="record the requirements-dev.txt tool pins without running the tools",
    )
    parser.add_argument(
        "--align-openspec-sizes",
        action="store_true",
        help="record the generated openspec-* skills' line counts, nothing else",
    )
    args = parser.parse_args()
    if args.align_openspec_sizes:
        sized = openspec_sized_baseline(
            BASELINE.read_text(encoding="utf-8"), instruction_lines(ROOT)
        )
        if sized is not None:
            BASELINE.write_text(sized, encoding="utf-8", newline="\n")
        print("aligned" if sized is not None else "unchanged")
        return 0
    if args.align_versions:
        aligned = aligned_baseline(
            BASELINE.read_text(encoding="utf-8"),
            REQUIREMENTS.read_text(encoding="utf-8"),
        )
        if aligned is not None:
            BASELINE.write_text(aligned, encoding="utf-8")
        print("aligned" if aligned is not None else "unchanged")
        return 0
    notes: dict[str, str] = {}
    versions, diagnostics = collect(ROOT, notes)
    if args.snapshot:
        if not args.base or not re.fullmatch(r"[0-9a-f]{40}", args.base):
            parser.error("--snapshot requires an immutable --base SHA")
        current = json.loads(BASELINE.read_text(encoding="utf-8"))
        BASELINE.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "base": args.base,
                    "versions": versions,
                    "diagnostics": [
                        identity(item) for item in sorted(diagnostics, key=identity_key)
                    ],
                    "coverage_evidence": current.get("coverage_evidence"),
                    "instruction_lines": instruction_lines(ROOT),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"Quality ratchet: wrote {BASELINE.relative_to(ROOT).as_posix()}")
        return 0
    return check(
        json.loads(BASELINE.read_text(encoding="utf-8")),
        versions,
        diagnostics,
        () if eslint_script() else ("eslint",),
        notes,
        instruction_lines(ROOT),
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError, KeyError, OSError) as error:
        print(f"Quality check failed: {error}", file=sys.stderr)
        sys.exit(2)
