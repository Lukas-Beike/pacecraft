"""Key/value service tests and the generated catalogue of the ``kv`` keys.

``docs/kv-catalogue.md`` lists every key or key pattern the backend writes or
deletes. The scan reads ``backend/**/*.py`` (and ``server.py``, which hands
callbacks to the backend): every SQL string that names kv is a root, and a key
that is a parameter is followed to the call sites of its function. Backend
functions are read, never run. Regenerate the document with
``python tests/test_key_value_service.py --write``.
"""

import ast
import builtins
import importlib
import itertools
import re
import runpy
import sys
import tempfile
import unittest
from collections import defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import Mock

# The repository root, for running this file as a script with --write.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.db.key_value import KeyValueService

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "kv-catalogue.md"
BEGIN = "<!-- BEGIN GENERATED KEYS -->\n"
END = "<!-- END GENERATED KEYS -->\n"

# Writes whose key the scan cannot read from the source: (file, function) to the
# reason. An entry needs a reason; an entry that no longer matches a write fails.
DYNAMIC_WRITES: dict[tuple[str, str], str] = {}

# `set`, `delete` and `get` name many unrelated methods, so the callers of the
# generic kv API are found by the receiver's name and the call's arity.
KV_CLASSES = ("KeyValue",)
KV_RECEIVER = re.compile(r"(key_?values?(_?repository|_service)?|kv)$", re.IGNORECASE)
BARE_RECEIVER = ("repository", "_repository")
DB_NAMES = ("db", "connection", "conn")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
MAX_DEPTH = 60
# The only calls the scan evaluates itself; backend functions are read, not run.
SAFE_CALLS = (tuple, list, set, frozenset, sorted, str, int)
KV_WRITE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+kv\b|\bUPDATE\s+kv\b", re.IGNORECASE
)
KV_DELETE = re.compile(r"\bDELETE\s+FROM\s+kv\b", re.IGNORECASE)
KV_READ = re.compile(r"\bFROM\s+kv\b", re.IGNORECASE)
KEY_PARAMETER = re.compile(
    r"substr\(\s*key\s*,\s*1\s*,\s*\?\s*\)\s*=\s*\?|\bkey\s*(?:=|LIKE)\s*\?",
    re.IGNORECASE,
)

# AST nodes carry `_parent` and `_mod`, set by `Mod`, so they are typed loosely.
Node = Any


@dataclass(frozen=True)
class V:
    """One possible value of an expression, and the function a key came from."""

    value: object
    origin: str | None = None


@dataclass(frozen=True)
class Site:
    """One resolved kv operation: set, delete, delete_prefix, read or bulk."""

    kind: str
    key: str
    writer: str
    path: str
    line: int


class Unresolved(Exception):
    def __init__(self, node: Node, reason: str):
        super().__init__(reason)
        self.node, self.reason = node, reason

    def location(self) -> tuple[str, str, int, str, str]:
        return (
            _module(self.node).path,
            _qualname(self.node),
            getattr(self.node, "lineno", 0),
            ast.unparse(self.node)[:70],
            self.reason,
        )


class Mod:
    """One parsed module; its namespace is imported only when a constant needs it."""

    def __init__(self, name: str, path: str, source: str, ns: dict | None = None):
        self.name, self.path, self.source, self._ns = name, path, source, ns
        self.tree: Node = ast.parse(source)
        self.tree._mod = self  # type: ignore[attr-defined]
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent  # type: ignore[attr-defined]

    @property
    def ns(self) -> dict:
        if self._ns is None:
            self._ns = vars(importlib.import_module(self.name))
        return self._ns


def backend_modules() -> list[Mod]:
    modules = []
    for file in sorted((ROOT / "backend").rglob("*.py")):
        relative = file.relative_to(ROOT)
        name = ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
        modules.append(Mod(name, relative.as_posix(), file.read_text(encoding="utf-8")))
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    return [*modules, Mod("server", "server.py", server)]


def synthetic_module(source: str) -> Mod:
    """A module built from a source snippet, for the scanner's self-test."""
    with tempfile.TemporaryDirectory() as directory:
        file = Path(directory) / "synthetic.py"
        file.write_text(source, encoding="utf-8")
        namespace = runpy.run_path(str(file), run_name="backend.synthetic")
    return Mod("backend.synthetic", "backend/synthetic.py", source, namespace)


def _ancestors(node: Node):
    while (node := getattr(node, "_parent", None)) is not None:
        yield node


def _module(node: Node) -> Mod:
    return next(a for a in (node, *_ancestors(node)) if hasattr(a, "_mod"))._mod


def _qualname(node: Node) -> str:
    names = [
        a.name
        for a in (node, *_ancestors(node))
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return ".".join(reversed(names)) or "<module>"


def _label(node: Node) -> str:
    return f"{_module(node).path}:{_qualname(node)}"


def _target_names(target: Node) -> set[str]:
    """The names an assignment or loop target binds."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {n for e in target.elts for n in _target_names(e)}
    return set()


def _bind(target: Node, value: object, name: str) -> object:
    """The part of an unpacked value that `name` receives."""
    if isinstance(target, ast.Name):
        return value
    try:
        for sub, item in zip(target.elts, value, strict=True):  # type: ignore[call-overload]
            if name in _target_names(sub):
                return _bind(sub, item, name)
    except (TypeError, ValueError, AttributeError) as error:
        raise Unresolved(target, "the value cannot be unpacked") from error
    raise Unresolved(target, "the name is not in the target")


def _callee_name(call: Node) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _receiver_tail(call: Node) -> str:
    node = getattr(call.func, "value", None)
    while isinstance(node, ast.Call):
        node = node.func
    return str(getattr(node, "id", None) or getattr(node, "attr", ""))


def _kv_receiver(call: Node) -> bool:
    """Whether a method call is on a kv repository or service."""
    tail = _receiver_tail(call)
    return bool(KV_RECEIVER.search(tail)) or (
        tail in BARE_RECEIVER and "KeyValueRepository" in _module(call).source
    )


def _placeholder(expr: Node) -> str:
    """The name a computed part of a key shows as: `<day>`, `<coverage.provider>`."""
    if (
        isinstance(expr, ast.Call)
        and _callee_name(expr) in ("str", "int")
        and expr.args
    ):
        return _placeholder(expr.args[0])
    if isinstance(expr, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
        text = ast.unparse(expr.func if isinstance(expr, ast.Call) else expr)
        return re.sub(r"[^\w.]+", "_", text).strip("_")
    return "x"


def _texts(values: list[V] | None) -> bool:
    return values is not None and all(isinstance(v.value, str) for v in values)


def _sql_text(node: Node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _sql_text(node.left), _sql_text(node.right)
        return None if left is None or right is None else left + right
    return None


def _plan(text: str):
    """What one SQL string does to kv: (kind, where the key comes from, detail)."""
    flat = " ".join(text.split())
    if KV_DELETE.search(flat):
        kind = "delete"
    elif KV_WRITE.search(flat):
        kind = "set"
    elif KV_READ.search(flat):
        kind = "read"
    else:
        return None
    if kind == "delete" and not re.search(r"\bWHERE\b", flat, re.IGNORECASE):
        return "bulk", "bulk", None
    literals = re.findall(r"\bkey\s*=\s*'([^']+)'", flat, re.IGNORECASE)
    for group in re.findall(r"\bkey\s+IN\s*\(([^)]*)\)", flat, re.IGNORECASE):
        literals += re.findall(r"'([^']+)'", group)
    if literals:
        return kind, "literal", literals
    if re.search(r"\bkv\s*\(\s*key\b", flat, re.IGNORECASE):
        return kind, "parameter", (0, flat.count("?") - 1)
    found = KEY_PARAMETER.search(flat)
    if found is None:
        return kind, "none", None
    substr = "substr" in found.group(0)
    if kind == "delete" and substr:
        kind = "delete_prefix"
    index = flat[: found.start()].count("?") + substr
    return kind, "parameter", (index, flat[found.end() :].count("?"))


class Scanner:
    """Every kv write, delete and read of the given modules, with its key."""

    def __init__(self, modules: list[Mod]):
        self.modules = modules
        self.calls: dict[str, list[Node]] = defaultdict(list)
        self.defs: dict[str, list[Node]] = defaultdict(list)
        self.passed: dict[str, list[Node]] = defaultdict(list)
        self.sites: dict[Site, None] = {}
        self.failures: dict[tuple, None] = {}
        self.statements = 0
        self._depth, self._soft, self._attr = 0, 0, True
        self._active: set[int] = set()
        self._memo: dict[tuple, tuple] = {}
        self._frames: list[tuple[Node, dict[str, list[V]]]] = []
        for mod in modules:
            for node in ast.walk(mod.tree):
                self._index(node)
        for mod in modules:
            for node in ast.walk(mod.tree):
                self._statement(node)

    def _index(self, node: Node) -> None:
        """Remember calls, definitions and the names handed on as arguments."""
        if isinstance(node, ast.Call) and (name := _callee_name(node)):
            self.calls[name].append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.defs[node.name].append(node)
        elif isinstance(node, ast.ClassDef):
            inits = [n for n in node.body if getattr(n, "name", "") == "__init__"]
            self.defs[node.name] += inits
        elif isinstance(node, (ast.Name, ast.Attribute)):
            parent = getattr(node, "_parent", None)
            if isinstance(parent, ast.keyword) or (
                isinstance(parent, ast.Call) and node in parent.args
            ):
                handed = str(getattr(node, "id", None) or getattr(node, "attr", ""))
                self.passed[handed].append(node)

    # -- roots: the SQL strings that name kv -------------------------------------

    def _statement(self, node: Node) -> None:
        text = _sql_text(node)
        parent = getattr(node, "_parent", None)
        if text is None or (parent is not None and _sql_text(parent) is not None):
            return
        if isinstance(parent, ast.Expr) or (plan := _plan(text)) is None:
            return
        self.statements += 1
        kind, source, detail = plan
        where, path = _label(node), _module(node).path
        line = getattr(node, "lineno", 0)
        if source == "bulk":
            self._add(Site("bulk", "*", where, path, line))
        elif source == "literal":
            self._add_all(kind, [V(key) for key in detail], where, path, line)
        elif source == "parameter":
            values = self._parameter(node, kind, detail)
            self._add_all(kind, values, where, path, line)
        elif kind != "read":
            self._fail(node, "the statement names no key")

    def _add_all(self, kind: str, values: list[V], where: str, path: str, line: int):
        for value in values:
            self._add(Site(kind, str(value.value), value.origin or where, path, line))

    def _parameter(self, node: Node, kind: str, detail: tuple[int, int]) -> list[V]:
        call: Node = getattr(node, "_parent", None)
        parameters = (
            call.args[1] if isinstance(call, ast.Call) and len(call.args) > 1 else None
        )
        if (
            not isinstance(parameters, (ast.Tuple, ast.List))
            or call.args[0] is not node
        ):
            self._fail(node, "the parameters are not a literal tuple")
            return []
        elements = parameters.elts
        starred = any(isinstance(e, ast.Starred) for e in elements)
        expression = elements[-1 - detail[1]] if starred else elements[detail[0]]
        return self._resolve(expression, record=kind != "read")

    def _add(self, site: Site) -> None:
        self.sites[site] = None

    def _fail(self, node: Node, reason: str) -> None:
        self.failures[Unresolved(node, reason).location()] = None

    def _resolve(self, expression: Node, record: bool) -> list[V]:
        saved = self.failures
        if not record:
            self.failures = {}
        try:
            values = self.ev(expression)
        except Unresolved as error:
            self.failures[error.location()] = None
            return []
        finally:
            self.failures = saved
        keys = []
        for value in values:
            text = value.value
            if text is None:
                continue
            if isinstance(text, str) and not re.fullmatch(r"(<[^>]*>)*", text):
                keys.append(value)
            else:
                reason = f"the key {text!r} has no literal part"
                saved[Unresolved(expression, reason).location()] = None
        return keys

    # -- expression evaluation ---------------------------------------------------

    def ev(self, node: Node) -> list[V]:
        """Every value an expression can take; Unresolved when one cannot be read."""
        self._depth += 1
        try:
            method = getattr(self, f"_ev_{type(node).__name__}", None)
            if method is None or self._depth > MAX_DEPTH:
                raise Unresolved(node, "not a literal, constant or f-string key")
            return method(node)
        finally:
            self._depth -= 1

    def _nested(self, node: Node) -> list[V]:
        """A part of a computed key: it resolves as a whole or the key does not."""
        self._soft += 1
        try:
            return self.ev(node)
        finally:
            self._soft -= 1

    def _try(self, node: Node) -> list[V] | None:
        try:
            return self._nested(node)
        except Unresolved:
            return None

    def _ev_Constant(self, node: Node) -> list[V]:
        return [V(node.value)]

    def _ev_JoinedStr(self, node: Node) -> list[V]:
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append([V(part.value)])
                continue
            values = self._try(part.value)
            if values is None or not all(
                isinstance(v.value, (str, int)) for v in values
            ):
                values = [V(f"<{_placeholder(part.value)}>")]
            parts.append(values)
        return [V("".join(str(v.value) for v in c)) for c in itertools.product(*parts)]

    def _ev_BinOp(self, node: Node) -> list[V]:
        if not isinstance(node.op, ast.Add):
            raise Unresolved(node, "not a literal, constant or f-string key")
        left, right = self._try(node.left), self._try(node.right)
        if left is None and right is None:
            raise Unresolved(node, "neither side of the key is known")
        # A part that is not text (an unknown value is None) is computed too.
        left = left if _texts(left) else [V(f"<{_placeholder(node.left)}>")]
        right = right if _texts(right) else [V(f"<{_placeholder(node.right)}>")]
        try:
            return [
                V(a.value + b.value)  # type: ignore[operator]
                for a, b in itertools.product(left or (), right or ())
            ]
        except TypeError as error:
            raise Unresolved(node, "the operands cannot be added") from error

    def _container(self, node: Node, elements: list[Node], build) -> list[V]:
        if any(isinstance(e, ast.Starred) for e in elements):
            raise Unresolved(node, "a starred element")
        options = [self._nested(e) for e in elements]
        return [V(build([v.value for v in c])) for c in itertools.product(*options)]

    def _ev_Tuple(self, node: Node) -> list[V]:
        return self._container(node, node.elts, tuple)

    def _ev_List(self, node: Node) -> list[V]:
        return self._container(node, node.elts, list)

    def _ev_Set(self, node: Node) -> list[V]:
        return self._container(node, node.elts, set)

    def _ev_Dict(self, node: Node) -> list[V]:
        if None in node.keys:
            raise Unresolved(node, "a dict unpacking")
        keys = [self._nested(k) for k in node.keys]
        # A value the scan cannot read is kept as None: only the keys are kv keys.
        values = [self._try(v) or [V(None)] for v in node.values]
        width = len(keys)
        return [
            V({k.value: v.value for k, v in zip(c[:width], c[width:], strict=True)})  # type: ignore[misc]
            for c in itertools.product(*keys, *values)
        ]

    def _ev_IfExp(self, node: Node) -> list[V]:
        return [*self.ev(node.body), *self.ev(node.orelse)]

    def _comprehension(self, node: Node) -> list[V]:
        """Every element a comprehension can produce, as one tuple."""
        return [V(tuple(v.value for v in self._nested(node.elt)))]

    _ev_GeneratorExp = _ev_ListComp = _ev_SetComp = _comprehension

    def _ev_Attribute(self, node: Node) -> list[V]:
        owner = node.value
        if isinstance(owner, ast.Name) and owner.id in ("self", "cls"):
            return self._self_attribute(node)
        try:
            return [V(getattr(v.value, node.attr)) for v in self._nested(owner)]
        except AttributeError as error:
            raise Unresolved(node, "the attribute does not exist") from error

    def _self_attribute(self, node: Node) -> list[V]:
        owner = next((a for a in _ancestors(node) if isinstance(a, ast.ClassDef)), None)
        namespace = _module(node).ns
        # A subclass that sets the attribute decides it; the base holds a default.
        overrides = [
            vars(namespace[c.name])[node.attr]
            for c in ast.walk(_module(node).tree)
            if isinstance(c, ast.ClassDef)
            and owner is not None
            and any(isinstance(b, ast.Name) and b.id == owner.name for b in c.bases)
            and node.attr in vars(namespace.get(c.name, type))
        ]
        if overrides:
            return [V(value) for value in overrides]
        cls = None if owner is None else namespace.get(owner.name)
        if cls is not None and hasattr(cls, node.attr):
            return [V(getattr(cls, node.attr))]
        assigned = [
            n.value
            for n in ast.walk(owner or node)
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == node.attr for t in n.targets
            )
        ]
        if not assigned:
            raise Unresolved(node, "the attribute is not set in its class")
        return [v for value in assigned for v in self.ev(value)]

    def _ev_Subscript(self, node: Node) -> list[V]:
        objects = self._nested(node.value)
        index = self._try(node.slice)
        out = []
        for obj in objects:
            if index is None and isinstance(obj.value, Mapping):
                out += [V(v) for v in obj.value.values()]
            elif index is None:
                raise Unresolved(node, "the index is not known")
            else:
                try:
                    out += [V(obj.value[i.value]) for i in index]  # type: ignore[index]
                except (KeyError, IndexError, TypeError) as error:
                    raise Unresolved(node, "the index is not in the value") from error
        if any(v.value is None for v in out):
            raise Unresolved(node, "the stored value is not known")
        return out

    def _ev_Call(self, node: Node) -> list[V]:
        callables = self._nested(node.func)
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords
        ):
            raise Unresolved(node, "a starred argument")
        out = []
        for function in callables:
            f: Any = function.value
            if f in SAFE_CALLS or (
                getattr(f, "__name__", "") in ("items", "values", "keys")
                and isinstance(getattr(f, "__self__", None), dict)
            ):
                positional = [self._nested(a) for a in node.args]
                out += [
                    V(f(*[a.value for a in args]))
                    for args in itertools.product(*positional)
                ]
            elif str(getattr(f, "__module__", "")).startswith("backend"):
                out += self._returned(node, f)
            else:
                raise Unresolved(node, "the call is not a key builder of backend")
        return out

    def _returned(self, node: Node, function: Any) -> list[V]:
        """A key builder of backend: what its return statements build from the call."""
        out = []
        for definition in self.defs.get(getattr(function, "__name__", ""), ()):
            if _module(definition).name != getattr(function, "__module__", None):
                continue
            bound = {}
            if isinstance(definition._parent, ast.Module):
                names = [a.arg for a in definition.args.args]
                arguments = [
                    *zip(names, node.args, strict=False),
                    *((k.arg, k.value) for k in node.keywords),
                ]
                for name, argument in arguments:
                    if (values := self._try(argument)) is not None:
                        bound[name] = values
            self._frames.append((definition, bound))
            try:
                out += [v for r in _returns(definition) for v in self._nested(r)]
            finally:
                self._frames.pop()
        if not out:
            raise Unresolved(node, "the call is not a key builder of backend")
        return out

    def _ev_Name(self, node: Node) -> list[V]:
        name, child = node.id, node
        for parent in _ancestors(node):
            if (found := self._bound_by(parent, child, node, name)) is not None:
                return found
            child = parent
        namespace = _module(node).ns
        if name in namespace:
            return [V(namespace[name])]
        if hasattr(builtins, name):
            return [V(getattr(builtins, name))]
        raise Unresolved(node, "the name is not a module constant")

    def _bound_by(
        self, parent: Node, child: Node, node: Node, name: str
    ) -> list[V] | None:
        """What `name` is where a loop, comprehension or function binds it."""
        loop = isinstance(parent, (ast.For, ast.AsyncFor, ast.comprehension))
        if loop and child is not parent.iter and name in _target_names(parent.target):
            return self._loop(parent.target, name, parent.iter)
        if isinstance(parent, COMPREHENSIONS) and not isinstance(
            child, ast.comprehension
        ):
            for gen in parent.generators:
                if name in _target_names(gen.target):
                    return self._loop(gen.target, name, gen.iter)
        if isinstance(parent, FUNCTIONS):
            return self._in_function(node, parent, name)
        return None

    def _in_function(self, node: Node, function: Node, name: str) -> list[V] | None:
        """What a name is inside a function: a local, a bound or a traced parameter."""
        local = not isinstance(function, ast.Lambda) and not _in_assignment_to(
            node, function, name
        )
        if local and (assigned := self._assigned(function, name)):
            return assigned
        for frame, bound in reversed(self._frames):
            if frame is function and name in bound:
                return bound[name]
        parameters = {a.arg for a in ast.walk(function.args) if isinstance(a, ast.arg)}
        if name in parameters:
            return self._parameter_values(function, name, node)
        return None

    def _loop(self, target: Node, name: str, iterable: Node) -> list[V]:
        saved, self._attr = self._attr, False
        try:
            iterables = self._nested(iterable)
        finally:
            self._attr = saved
        out = []
        for value in iterables:
            items: Any = value.value
            if isinstance(items, (str, bytes)) or not hasattr(items, "__iter__"):
                raise Unresolved(iterable, "the loop is not over a known collection")
            out += [V(_bind(target, item, name)) for item in items]
        return out

    def _assigned(self, function: Node, name: str) -> list[V]:
        out: list[V] = []
        pending = list(ast.iter_child_nodes(function))
        while pending:
            node = pending.pop()
            if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
                continue
            pending.extend(ast.iter_child_nodes(node))
            out += self._binding(node, name)
        return self._with_stored_keys(function, name, out)

    def _binding(self, node: Node, name: str) -> list[V]:
        """The values one statement or walrus binds to `name`."""
        if isinstance(node, ast.AugAssign) and name in _target_names(node.target):
            raise Unresolved(node, "the name is changed by an augmented assignment")
        if isinstance(node, ast.NamedExpr) and node.target.id == name:
            return self.ev(node.value)
        targets = [t for t in getattr(node, "targets", ()) if name in _target_names(t)]
        if isinstance(node, ast.Assign) and targets:
            return [V(_bind(targets[0], v.value, name)) for v in self.ev(node.value)]
        return []

    def _with_stored_keys(self, function: Node, name: str, out: list[V]) -> list[V]:
        """Add the keys stored into a dict after its literal: `values["key"] = x`."""
        stored = [
            n.slice
            for n in ast.walk(function)
            if isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Store)
            and isinstance(n.value, ast.Name)
            and n.value.id == name
        ]
        if not stored:
            return out
        extra = [v.value for s in stored for v in self._nested(s)]
        return [
            V({**v.value, **dict.fromkeys(extra)})  # type: ignore[dict-item]
            if isinstance(v.value, dict)
            else v
            for v in out
        ]

    # -- following a key parameter to the call sites of its function --------------

    def _parameter_values(self, function: Node, name: str, node: Node) -> list[V]:
        memo = (id(function), name, self._attr, bool(self._soft))
        if memo not in self._memo:
            saved, self.failures = self.failures, {}
            try:
                outcome: tuple = (self._follow(function, name, node), None)
            except Unresolved as error:
                outcome = (None, error)
            collected, self.failures = self.failures, saved
            self._memo[memo] = (*outcome, collected)
        values, failure, failures = self._memo[memo]
        self.failures.update(failures)
        if failure is not None:
            raise failure
        return values

    def _follow(self, function: Node, name: str, node: Node) -> list[V]:
        arguments = (
            [] if isinstance(function, ast.Lambda) else self._arguments(function, name)
        ) or self._callback_arguments(function, name)
        if not arguments:
            raise Unresolved(node, "no call site in backend passes the key")
        out = []
        for origin, argument in arguments:
            if id(argument) in self._active:
                continue
            self._active.add(id(argument))
            try:
                values = self.ev(argument)
            except Unresolved as error:
                if self._soft:
                    raise
                self.failures[error.location()] = None
                continue
            finally:
                self._active.discard(id(argument))
            label = _label(origin)
            out += [
                replace(v, origin=v.origin or label) if self._attr else v
                for v in values
            ]
        return out

    def _arguments(self, function: Node, name: str) -> list[tuple[Node, Node]]:
        """The (origin, argument) pairs every accepted call passes for a parameter."""
        args = function.args
        parameters = [a.arg for a in (*args.posonlyargs, *args.args)]
        owner = function._parent
        in_class = isinstance(owner, ast.ClassDef)
        static = any(
            isinstance(d, ast.Name) and d.id == "staticmethod"
            for d in function.decorator_list
        )
        offset = 1 if in_class and parameters and not static else 0
        called = (
            owner.name if function.name == "__init__" and in_class else function.name
        )
        total = (
            1 << 30 if args.vararg else len(parameters) - offset + len(args.kwonlyargs)
        )
        required = len(parameters) - offset - len(args.defaults)
        required += sum(d is None for d in args.kw_defaults)
        out = []
        for call in self.calls.get(called, ()):
            count = len(call.args) + len(call.keywords)
            accepted = required <= count <= total and self._calls(call, function, owner)
            if accepted and (pair := _bound_argument(call, function, name, offset)):
                out.append(pair)
        return out

    def _callback_arguments(self, function: Node, name: str) -> list[tuple[Node, Node]]:
        """The arguments a function handed on as a callback is called with."""
        offset = 1 if isinstance(function._parent, ast.ClassDef) else 0
        positional = [a.arg for a in function.args.args]
        if name not in positional:
            return []
        wanted = positional.index(name) - offset
        references = (
            [function]
            if isinstance(function, ast.Lambda)
            else self.passed.get(function.name, [])
        )
        return [pair for ref in references for pair in self._callbacks(ref, wanted)]

    def _callbacks(self, reference: Node, wanted: int) -> list[tuple[Node, Node]]:
        holder = reference._parent
        keyword = holder.arg if isinstance(holder, ast.keyword) else None
        call = holder._parent if keyword else holder
        if not isinstance(call, ast.Call) or (callee := _callee_name(call)) is None:
            return []
        position = call.args.index(reference) if keyword is None else -1
        out = []
        for function in self.defs.get(callee, ()):
            owner = function._parent
            offset = 1 if isinstance(owner, ast.ClassDef) else 0
            positional = [a.arg for a in function.args.args]
            named = [*positional, *(a.arg for a in function.args.kwonlyargs)]
            if keyword is None and position + offset < len(positional):
                parameter = positional[position + offset]
            elif keyword in named:
                parameter = keyword
            else:
                continue
            scope = owner if isinstance(owner, ast.ClassDef) else function
            out += _calls_of(scope, function, parameter, wanted)
        return out

    def _calls(self, call: Node, function: Node, owner: Node) -> bool:
        """Whether a call to the function's name is a call of this function."""
        if isinstance(owner, ast.ClassDef) and owner.name.startswith(KV_CLASSES):
            return _calls_kv_method(call, function)
        if isinstance(owner, ast.ClassDef):
            return self._calls_method(call, function, owner)
        if isinstance(owner, ast.Module):
            target: Any = _module(call).ns
            for part in ast.unparse(call.func).split("."):
                target = (
                    target.get(part)
                    if isinstance(target, dict)
                    else getattr(target, part, None)
                )
            return (
                getattr(target, "__name__", None) == function.name
                and getattr(target, "__module__", None) == _module(function).name
            )
        return owner in _ancestors(call)

    def _calls_method(self, call: Node, function: Node, owner: Node) -> bool:
        receiver = getattr(call.func, "value", None)
        if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
            return owner in _ancestors(call)
        if function.name == "__init__" or len(self.defs[function.name]) == 1:
            return True
        # A name several classes define: the receiver must resemble the class.
        tokens = [t.rstrip("s") for t in _receiver_tail(call).split("_") if len(t) > 2]
        return any(t.lower() in owner.name.lower() for t in tokens)

    # -- queries ----------------------------------------------------------------

    def module(self, name: str) -> Mod:
        return next(m for m in self.modules if m.name == name)

    def kv_set_keys(self, path: str, function: str, after: int = 0) -> list[str]:
        """The keys the kv `set` calls of one function write after a line."""
        out = []
        for call in self.calls["set"]:
            if (
                _module(call).path == path
                and _qualname(call) == function
                and call.lineno > after
                and len(call.args) == 3
                and _kv_receiver(call)
            ):
                out += [str(v.value) for v in self.ev(call.args[1])]
        return out

    def unexplained(self) -> list[tuple]:
        """Failures no DYNAMIC_WRITES entry names."""
        return [f for f in self.failures if (f[0], f[1]) not in DYNAMIC_WRITES]


def _returns(definition: Node) -> list[Node]:
    """The values a function returns, not those of the functions inside it."""
    out, pending = [], list(ast.iter_child_nodes(definition))
    while pending:
        node = pending.pop()
        if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
            continue
        pending.extend(ast.iter_child_nodes(node))
        if isinstance(node, ast.Return) and node.value is not None:
            out.append(node.value)
    return out


def _in_assignment_to(node: Node, function: Node, name: str) -> bool:
    """Whether the node is part of the value of an assignment that binds `name`."""
    for ancestor in _ancestors(node):
        if ancestor is function:
            return False
        if isinstance(ancestor, ast.Assign) and any(
            name in _target_names(t) for t in ancestor.targets
        ):
            return True
    return False


def _bound_argument(call: Node, function: Node, name: str, offset: int):
    """The (origin, argument) a call passes for a parameter, or None."""
    args = function.args
    parameters = [a.arg for a in (*args.posonlyargs, *args.args)]
    if args.vararg and name == args.vararg.arg:
        rest = ast.Tuple(elts=call.args[len(parameters) - offset :])
        rest._parent = call  # type: ignore[attr-defined]
        return call, rest
    index = parameters.index(name) - offset if name in parameters else -1
    if 0 <= index < len(call.args) and not any(
        isinstance(a, ast.Starred) for a in call.args[: index + 1]
    ):
        return call, call.args[index]
    if keyword := next((k for k in call.keywords if k.arg == name), None):
        return call, keyword.value
    default = _default(function, name)
    return None if default is None else (function, default)


def _default(function: Node, name: str) -> Node | None:
    args = function.args
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    if name in positional:
        index = positional.index(name) - (len(positional) - len(args.defaults))
        return args.defaults[index] if index >= 0 else None
    names = [a.arg for a in args.kwonlyargs]
    return args.kw_defaults[names.index(name)] if name in names else None


def _calls_kv_method(call: Node, function: Node) -> bool:
    """A call of a KeyValueRepository or KeyValueService method."""
    first = [a.arg for a in function.args.args][1]
    handed_db = (
        bool(call.args)
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id in DB_NAMES
    )
    return (
        isinstance(call.func, ast.Attribute)
        and _kv_receiver(call)
        and not (first not in DB_NAMES and handed_db)
    )


def _calls_of(scope: Node, function: Node, parameter: str, wanted: int):
    """The calls of a callback parameter (or of the attribute it is stored in)."""
    attributes = {
        t.attr
        for n in ast.walk(function)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Name)
        and n.value.id == parameter
        for t in n.targets
        if isinstance(t, ast.Attribute)
    }
    out = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call) or wanted >= len(node.args):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == parameter) or (
            isinstance(func, ast.Attribute) and func.attr in attributes
        ):
            out.append((node, node.args[wanted]))
    return out


@cache
def real_scanner() -> Scanner:
    return Scanner(backend_modules())


# -- the catalogue ------------------------------------------------------------


def export_rules(scanner: Scanner) -> tuple[set[str], tuple[str, ...]]:
    """The keys and key suffixes the privacy export leaves out."""
    excluded: set[str] = set()
    suffixes: tuple[str, ...] = ()
    for node in ast.walk(scanner.module("backend.backup.export").tree):
        if isinstance(node, ast.FunctionDef) and node.name == "application_state":
            names = [a.arg for a in node.args.kwonlyargs]
            default = node.args.kw_defaults[names.index("excluded_suffixes")]
            suffixes = tuple(ast.literal_eval(default))  # type: ignore[arg-type]
        if isinstance(node, ast.keyword) and node.arg == "excluded_keys":
            excluded |= _evaluated_set(scanner, node.value)
    return excluded, suffixes


def _evaluated_set(scanner: Scanner, literal: Node) -> set[str]:
    """The strings of a set literal whose elements may be constants or `*sets`."""
    out: set[str] = set()
    for element in literal.elts:
        starred = isinstance(element, ast.Starred)
        for v in scanner.ev(element.value if starred else element):
            out |= set(v.value) if starred else {str(v.value)}  # type: ignore[call-overload]
    return out


def rules(scanner: Scanner | None) -> dict:
    """What the export, startup and the purge do to keys, read from their code."""
    if scanner is None:
        return {"excluded": set(), "suffixes": (), "startup": set(), "purge": set()}
    privacy = "backend/privacy.py"
    bulk = [s for s in scanner.sites if s.kind == "bulk" and s.path == privacy]
    excluded, suffixes = export_rules(scanner)
    startup = scanner.kv_set_keys("backend/db/bootstrap.py", "apply_startup_invariants")
    purge = {
        key
        for s in bulk
        for key in scanner.kv_set_keys(privacy, s.writer.split(":", 1)[1], s.line)
    }
    return {
        "excluded": excluded,
        "suffixes": suffixes,
        "startup": set(startup),
        "purge": purge,
    }


def _key_pattern(key: str) -> re.Pattern[str]:
    parts = re.split(r"(<[^>]*>)", key)
    return re.compile(
        "".join(".+" if i % 2 else re.escape(p) for i, p in enumerate(parts)),
        re.DOTALL,
    )


def read_never_written(scanner: Scanner, known: list[str]) -> list[str]:
    patterns = [_key_pattern(k) for k in known]
    missing = set()
    for site in scanner.sites:
        if site.kind != "read" or "<" in site.key:
            continue
        key = site.key.removesuffix("%")
        if site.key.endswith("%") and any(k.startswith(key) for k in known):
            continue
        if not any(p.fullmatch(key) for p in patterns):
            missing.add(site.key)
    return sorted(missing)


def _row(key: str, writers: set[str], removed: set[str], rule: dict) -> str:
    out = key in rule["excluded"] or key.endswith(rule["suffixes"])
    return (
        f"| `{key}` | {'<br>'.join(sorted(writers)) or '-'} "
        f"| {'<br>'.join(sorted(removed)) or '-'} | {'no' if out else 'yes'} "
        f"| {'yes' if key in rule['startup'] else '-'} "
        f"| {'rewritten' if key in rule['purge'] else '-'} |"
    )


def _rows(scanner: Scanner, rule: dict) -> tuple[list[str], list[str], dict]:
    written: dict[str, set[str]] = defaultdict(set)
    deleted: dict[str, set[str]] = defaultdict(set)
    prefixes: dict[str, set[str]] = defaultdict(set)
    kinds = {"set": written, "delete": deleted, "delete_prefix": prefixes}
    for site in scanner.sites:
        if site.kind in kinds:
            kinds[site.kind][site.key].add(site.writer)
    keys = sorted({*written, *deleted})
    rows = []
    for key in keys:
        removed = deleted[key].union(
            *(w for prefix, w in prefixes.items() if key.startswith(prefix))
        )
        rows.append(_row(key, written[key], removed, rule))
    return keys, rows, prefixes


def _notes(scanner: Scanner, rule: dict, keys: list[str], prefixes: dict) -> list[str]:
    lines: list[str] = []
    unmatched = sorted(p for p in prefixes if not any(k.startswith(p) for k in keys))
    for prefix in unmatched:
        lines += [
            f"- `{prefix}...` is deleted by {', '.join(sorted(prefixes[prefix]))}"
        ]
    lines += [
        f"- `{path}:{function}` writes keys the scan cannot read: {reason}"
        for (path, function), reason in sorted(DYNAMIC_WRITES.items())
    ]
    wiped = sorted({s.writer for s in scanner.sites if s.kind == "bulk"})
    lines += [f"- every key is deleted by `{writer}`" for writer in wiped]
    silent = sorted(rule["excluded"] - set(keys))
    lines += [
        f"- `{key}` is left out of the export; nothing writes it" for key in silent
    ]
    never = read_never_written(scanner, keys)
    lines += [f"- `{key}` is read but never written" for key in never]
    return lines


def catalogue(scanner: Scanner, rule: dict) -> str:
    keys, rows, prefixes = _rows(scanner, rule)
    patterns = sum("<" in k for k in keys)
    lines = [
        f"{len(keys) - patterns} keys and {patterns} key patterns; "
        + "`<name>` stands for a value the code computes.",
        "",
        "| Key | Written by | Deleted by | Export | Reset at start | Purge |",
        "| --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
        *(_notes(scanner, rule, keys, prefixes) or ["Nothing else to note."]),
    ]
    return "\n".join(lines) + "\n"


def rendered() -> str:
    scanner = real_scanner()
    return catalogue(scanner, rules(scanner))


class KeyValueServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = object()

        class Database:
            @contextmanager
            def unit_of_work(inner_self):
                yield self.db

            @contextmanager
            def reader(inner_self):
                yield self.reader_db

        self.reader_db = object()
        self.repository = Mock()
        self.service = KeyValueService(Database(), self.repository)

    def test_get_reads_on_a_reader_without_the_writer(self):
        self.repository.get.return_value = "stored"

        self.assertEqual(self.service.get("setting"), "stored")

        self.repository.get.assert_called_once_with(self.reader_db, "setting")

    def test_set_writes_inside_database_unit_of_work(self):
        self.service.set("setting", "value")

        self.repository.set.assert_called_once_with(self.db, "setting", "value")


SYNTHETIC_SAVE = (
    "PREFIX = 'day:'\n"
    "def write(db, key):\n"
    '    db.execute("DELETE FROM kv WHERE key = ?", (key,))\n'
    "def save(db, day):\n"
    "    db.execute(\n"
    '        "INSERT OR REPLACE INTO kv(key,value,updated_at) VALUES(?,?,?)",\n'
    '        ("brand_new_key", "v", "t"),\n'
    "    )\n"
    '    db.execute("UPDATE kv SET value=? WHERE key=?", ("v", PREFIX + day))\n'
    '    write(db, "removed_key")\n'
)
SYNTHETIC_UNREADABLE = (
    "def save(db):\n"
    "    db.execute(\n"
    '        "INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)",\n'
    '        (compute_key(), "v", "t"),\n'
    "    )\n"
)


class KeyValueCatalogueTests(unittest.TestCase):
    def test_generated_catalogue_is_current(self):
        document = DOC.read_text(encoding="utf-8")
        self.assertEqual(
            document.split(BEGIN, 1)[1].split(END, 1)[0],
            rendered(),
            "Regenerate with: python tests/test_key_value_service.py --write",
        )

    def test_every_kv_write_has_a_resolved_key(self):
        scanner = real_scanner()
        self.assertGreater(scanner.statements, 10)
        self.assertEqual(scanner.unexplained(), [])
        stale = set(DYNAMIC_WRITES) - {(f[0], f[1]) for f in scanner.failures}
        self.assertEqual(sorted(stale), [])

    def test_scanner_rejects_a_key_it_cannot_resolve(self):
        scanner = Scanner([synthetic_module(SYNTHETIC_UNREADABLE)])

        self.assertEqual([f[1] for f in scanner.unexplained()], ["save"])

    def test_scanner_lists_a_new_key_in_the_catalogue(self):
        scanner = Scanner([synthetic_module(SYNTHETIC_SAVE)])

        self.assertEqual(scanner.unexplained(), [])
        document = catalogue(scanner, rules(None))
        self.assertIn("| `brand_new_key` | backend/synthetic.py:save |", document)
        self.assertIn("`day:<day>`", document)
        self.assertIn("| `removed_key` | - | backend/synthetic.py:save |", document)


if __name__ == "__main__":
    if "--write" in sys.argv:
        text = DOC.read_text(encoding="utf-8")
        head, rest = text.split(BEGIN, 1)
        DOC.write_text(
            head + BEGIN + rendered() + END + rest.split(END, 1)[1],
            encoding="utf-8",
            newline="\n",
        )
    else:
        unittest.main()
