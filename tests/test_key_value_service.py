"""Key/value service tests and the generated catalogue of the ``kv`` keys.

``docs/kv-catalogue.md`` lists every key or key pattern the backend writes or
deletes. The scan reads ``backend/**/*.py`` and ``server.py`` (which hands
callbacks to the backend): every SQL string that writes kv is a root, and a key
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

DB_NAMES = ("db", "connection", "conn")
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
LOOPS = (ast.For, ast.AsyncFor, ast.comprehension)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
# The only calls the scan evaluates itself; backend functions are read, not run.
SAFE_CALLS = (tuple, list, set, frozenset, sorted, str, int)
WRITE = r"\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+kv\b|\bUPDATE\s+kv\b"
KEY_PARAMETER = r"substr\(\s*key\s*,\s*1\s*,\s*\?\s*\)\s*=\s*\?|\bkey\s*=\s*\?"

# AST nodes carry `_parent`, set by `Mod`, so they are typed loosely.
Node = Any


@dataclass(frozen=True)
class V:
    """One possible value of an expression; origin is the (function, line) of
    the call that supplied a key."""

    value: object
    origin: tuple[str, int] | None = None


@dataclass(frozen=True)
class Site:
    """One resolved kv operation: set, delete, delete_prefix or bulk."""

    kind: str
    key: str
    writer: str
    line: int


class Unresolved(Exception):
    def __init__(self, node: Node, reason: str):
        super().__init__(reason)
        self.node, self.reason = node, reason

    def location(self) -> tuple[str, str, int, str, str]:
        node = self.node
        return (
            _module(node).path,
            _qualname(node),
            getattr(node, "lineno", 0),
            ast.unparse(node)[:70],
            self.reason,
        )


class Mod:
    """A parsed module; its namespace is imported only when a constant needs it."""

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
    files = [*sorted((ROOT / "backend").rglob("*.py")), ROOT / "server.py"]
    return [
        Mod(
            ".".join(f.relative_to(ROOT).with_suffix("").parts).removesuffix(
                ".__init__"
            ),
            f.relative_to(ROOT).as_posix(),
            f.read_text(encoding="utf-8"),
        )
        for f in files
    ]


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
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = [a.name for a in (node, *_ancestors(node)) if isinstance(a, kinds)]
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
    except (TypeError, ValueError) as error:
        raise Unresolved(target, "the value cannot be unpacked") from error
    raise Unresolved(target, "the name is not in the target")


def _callee_name(call: Node) -> str | None:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _placeholder(expr: Node) -> str:
    """How a computed part of a key shows: `<day>`, `<coverage.provider>`."""
    if isinstance(expr, ast.Call) and _callee_name(expr) in ("str", "int"):
        return _placeholder(expr.args[0])
    text = ast.unparse(expr.func if isinstance(expr, ast.Call) else expr)
    return re.sub(r"[^\w.]+", "_", text).strip("_")


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
    """What one SQL string does to kv: (kind, literal keys, key parameter index)."""
    flat = " ".join(text.split())
    if re.search(r"\bDELETE\s+FROM\s+kv\b", flat, re.IGNORECASE):
        kind = "delete"
    elif re.search(WRITE, flat, re.IGNORECASE):
        kind = "set"
    else:
        return None
    if kind == "delete" and not re.search(r"\bWHERE\b", flat, re.IGNORECASE):
        return "bulk", ["*"], None
    keys = re.findall(r"\bkey\s*=\s*'([^']+)'", flat, re.IGNORECASE)
    for group in re.findall(r"\bkey\s+IN\s*\(([^)]*)\)", flat, re.IGNORECASE):
        keys += re.findall(r"'([^']+)'", group)
    if re.search(r"\bkv\s*\(\s*key\b", flat, re.IGNORECASE):
        return kind, keys, (0, flat.count("?") - 1)
    found = re.search(KEY_PARAMETER, flat, re.IGNORECASE)
    if keys or found is None:
        return kind, keys, None
    prefix = "substr" in found.group(0)
    index = (flat[: found.start()].count("?") + prefix, flat[found.end() :].count("?"))
    return ("delete_prefix" if prefix else kind), keys, index


class Scanner:
    """Every kv write and delete of the given modules, with its key."""

    def __init__(self, modules: list[Mod]):
        self.modules = modules
        self.calls: dict[str, list[Node]] = defaultdict(list)
        self.defs: dict[str, list[Node]] = defaultdict(list)
        self.passed: dict[str, list[Node]] = defaultdict(list)
        self.sites: dict[Site, None] = {}
        self.failures: dict[tuple, None] = {}
        self.statements = 0
        self._depth, self._soft, self._shallow, self._attr = 0, 0, 0, True
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
            parent: Node = getattr(node, "_parent", None)
            if isinstance(parent, ast.keyword) or (
                isinstance(parent, ast.Call) and node in parent.args
            ):
                handed = getattr(node, "id", None) or getattr(node, "attr", "")
                self.passed[str(handed)].append(node)

    # -- roots: the SQL strings that write kv --------------------------------------

    def _statement(self, node: Node) -> None:
        text, parent = _sql_text(node), getattr(node, "_parent", None)
        if text is None or isinstance(parent, ast.Expr):
            return
        if parent is not None and _sql_text(parent) is not None:
            return
        if (plan := _plan(text)) is None:
            return
        self.statements += 1
        kind, keys, index = plan
        if keys:
            values = [V(k) for k in keys]
        elif index is not None:
            values = self._key_values(node, index)
        else:
            values = []
            self._fail(node, "the statement names no key")
        for v in values:
            writer, line = v.origin or (_label(node), node.lineno)
            self.sites[Site(kind, str(v.value), writer, line)] = None

    def _key_values(self, node: Node, index: tuple[int, int]) -> list[V]:
        call: Node = node._parent
        params = call.args[1] if isinstance(call, ast.Call) and call.args[1:] else None
        if not isinstance(params, (ast.Tuple, ast.List)) or call.args[0] is not node:
            self._fail(node, "the parameters are not a literal tuple")
            return []
        starred = any(isinstance(e, ast.Starred) for e in params.elts)
        expression = params.elts[-1 - index[1] if starred else index[0]]
        try:
            values = self.ev(expression)
        except Unresolved as error:
            self.failures[error.location()] = None
            return []
        keys = []
        for v in values:
            if isinstance(v.value, str) and not re.fullmatch(r"(<[^>]*>)*", v.value):
                keys.append(v)
            elif v.value is not None:
                self._fail(expression, f"the key {v.value!r} has no literal part")
        return keys

    def _fail(self, node: Node, reason: str) -> None:
        self.failures[Unresolved(node, reason).location()] = None

    # -- expression evaluation ---------------------------------------------------

    def ev(self, node: Node) -> list[V]:
        """Every value an expression can take; Unresolved when one cannot be read."""
        self._depth += 1
        try:
            method = getattr(self, f"_ev_{type(node).__name__}", None)
            if method is None or self._depth > 60:
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
            found = self._try(part.value)
            text = bool(found) and all(
                isinstance(v.value, (str, int)) for v in found or ()
            )
            parts.append(
                found if text and found else [V(f"<{_placeholder(part.value)}>")]
            )
        return [V("".join(str(v.value) for v in c)) for c in itertools.product(*parts)]

    def _ev_BinOp(self, node: Node) -> list[V]:
        sides = [self._try(node.left), self._try(node.right)]
        if not isinstance(node.op, ast.Add) or sides == [None, None]:
            raise Unresolved(node, "neither side of the key is known")
        # A side that is not text (an unknown value is None) is computed too.
        left, right = (
            side
            if side and all(isinstance(v.value, str) for v in side)
            else [V(f"<{_placeholder(part)}>")]
            for side, part in zip(sides, (node.left, node.right), strict=True)
        )
        return [V(a.value + b.value) for a, b in itertools.product(left, right)]  # type: ignore[operator]

    def _container(self, node: Node, build) -> list[V]:
        if any(isinstance(e, ast.Starred) for e in node.elts):
            raise Unresolved(node, "a starred element")
        options = [self._nested(e) for e in node.elts]
        return [V(build([v.value for v in c])) for c in itertools.product(*options)]

    def _ev_Tuple(self, node: Node) -> list[V]:
        return self._container(node, tuple)

    def _ev_List(self, node: Node) -> list[V]:
        return self._container(node, list)

    def _ev_Set(self, node: Node) -> list[V]:
        return self._container(node, set)

    def _ev_Dict(self, node: Node) -> list[V]:
        """A dict literal: only its keys matter, an unreadable value is None."""
        if None in node.keys:
            raise Unresolved(node, "a dict unpacking")
        keys = [self._nested(k) for k in node.keys]
        return [V(dict.fromkeys(v.value for v in c)) for c in itertools.product(*keys)]  # type: ignore[arg-type]

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
        """`self.x`: what any class of the module sets `x` to, an empty default aside."""
        out = []
        nodes: list[Node] = list(ast.walk(_module(node).tree))
        for n in nodes:
            targets = getattr(n, "targets", None) or [getattr(n, "target", None)]
            is_class_body = isinstance(getattr(n, "_parent", None), ast.ClassDef)
            if getattr(n, "value", None) is not None and any(
                (isinstance(t, ast.Name) and is_class_body and t.id == node.attr)
                or (isinstance(t, ast.Attribute) and t.attr == node.attr)
                for t in targets
            ):
                out += [v for v in self.ev(n.value) if v.value]
        owner = next((a for a in _ancestors(node) if isinstance(a, ast.ClassDef)), None)
        method = getattr(
            _module(node).ns.get(owner.name if owner else ""), node.attr, None
        )
        if not out and method is None:
            raise Unresolved(node, "the attribute is not set in its class")
        return out or [V(method)]

    def _ev_Subscript(self, node: Node) -> list[V]:
        index = [i.value for i in self._try(node.slice) or []]
        out = []
        for owner in self._nested(node.value):
            table: Any = owner.value
            if isinstance(table, dict):
                # An index that is unknown or not in a dict stands for all its values.
                found = [table[i] for i in index if isinstance(i, str) and i in table]
                out += [V(v) for v in found or table.values()]
                continue
            try:
                out += [V(table[i]) for i in index]
            except (IndexError, KeyError, TypeError) as error:
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
                try:
                    out += [
                        V(f(*[a.value for a in c]))
                        for c in itertools.product(*positional)
                    ]
                except (TypeError, ValueError) as error:
                    raise Unresolved(node, "the argument is not known") from error
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
            names = [a.arg for a in definition.args.args]
            given = [*zip(names, node.args, strict=False)]
            given += [(k.arg, k.value) for k in node.keywords]
            bound = {n: v for n, a in given if (v := self._try(a)) is not None}
            self._frames.append((definition, bound))
            try:
                pending = list(ast.iter_child_nodes(definition))
                while pending:
                    inner = pending.pop()
                    if isinstance(inner, (*FUNCTIONS, ast.ClassDef)):
                        continue
                    pending.extend(ast.iter_child_nodes(inner))
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        out += self._nested(inner.value)
            finally:
                self._frames.pop()
        if not out:
            raise Unresolved(node, "the call is not a key builder of backend")
        return out

    def _ev_Name(self, node: Node) -> list[V]:
        name, child = node.id, node
        for parent in _ancestors(node):
            found = self._bound_by(parent, child, node, name)
            if found is not None:
                return found
            child = parent
        namespace = {**vars(builtins), **_module(node).ns}
        if name in namespace:
            return [V(namespace[name])]
        raise Unresolved(node, "the name is not a module constant")

    def _bound_by(self, parent: Node, child: Node, node: Node, name: str):
        """What `name` is where a loop, comprehension or function binds it."""
        loops: list[Node] = [parent] if isinstance(parent, LOOPS) else []
        if isinstance(parent, COMPREHENSIONS) and child not in parent.generators:
            loops = parent.generators
        for loop in loops:
            if child is not loop.iter and name in _target_names(loop.target):
                return self._loop(loop.target, name, loop.iter)
        if isinstance(parent, FUNCTIONS):
            return self._in_function(node, parent, name)
        return None

    def _in_function(self, node: Node, function: Node, name: str) -> list[V] | None:
        """A name inside a function: a local, a bound or a traced parameter."""
        inside = any(
            isinstance(a, ast.Assign) and name in _target_names(a.targets[0])
            for a in _ancestors(node)
            if a is not function
        )
        local = not isinstance(function, ast.Lambda) and not inside
        if local and (assigned := self._assigned(function, name)):
            return assigned
        for frame, bound in reversed(self._frames):
            if frame is function and name in bound:
                return bound[name]
        if name in {a.arg for a in ast.walk(function.args) if isinstance(a, ast.arg)}:
            if self._shallow:
                raise Unresolved(node, "a parameter is not followed here")
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
        """What assignments inside a function (not its inner functions) give a name."""
        out: list[V] = []
        stored: list[Node] = []
        pending: list[Node] = list(ast.iter_child_nodes(function))
        while pending:
            node = pending.pop()
            if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
                continue
            pending.extend(ast.iter_child_nodes(node))
            targets = getattr(node, "targets", None) or [getattr(node, "target", None)]
            bound = [t for t in targets if t is not None and name in _target_names(t)]
            if isinstance(node, ast.AugAssign) and bound:
                raise Unresolved(node, "the name is changed by an augmented assignment")
            if isinstance(node, (ast.NamedExpr, ast.Assign)) and bound:
                out += [V(_bind(bound[0], v.value, name)) for v in self.ev(node.value)]
            if _stores_into(node, name):
                stored.append(node.slice)  # a key added to a dict: `values["key"] = x`
        extra = [v.value for s in stored for v in self._nested(s)]
        return [
            V({**v.value, **dict.fromkeys(extra)}) if isinstance(v.value, dict) else v  # type: ignore[dict-item]
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
        arguments = self._arguments(function, name) or self._callbacks(function, name)
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
            where = (_label(origin), origin.lineno)
            out += [
                replace(v, origin=v.origin or where) if self._attr else v
                for v in values
            ]
        return out

    def _arguments(self, function: Node, name: str) -> list[tuple[Node, Node]]:
        """The (call, argument) pairs of the calls that pass a parameter."""
        if isinstance(function, ast.Lambda):
            return []
        args, owner = function.args, function._parent
        parameters = [a.arg for a in (*args.posonlyargs, *args.args)]
        method = isinstance(owner, ast.ClassDef) and "staticmethod" not in ast.unparse(
            function.decorator_list
        )
        offset = int(method and bool(parameters))
        called = owner.name if function.name == "__init__" else function.name
        total = (
            1 << 30 if args.vararg else len(parameters) - offset + len(args.kwonlyargs)
        )
        required = len(parameters) - offset - len(args.defaults)
        required += sum(d is None for d in args.kw_defaults)
        out = []
        for call in self.calls.get(called, ()):
            count = len(call.args) + len(call.keywords)
            fits = required <= count <= total
            match = self._calls(call, function, parameters) if fits else False
            pair = None
            if match is not False:
                pair = _bound_argument(call, function, name, offset)
            # A call that only has the shape of the method is taken when its
            # argument is a readable key.
            if pair and (match is True or self._readable(pair[1])):
                out.append(pair)
        return out

    def _readable(self, argument: Node) -> bool:
        """Whether an argument is a key without following any parameter."""
        self._shallow += 1
        try:
            return self._try(argument) is not None
        finally:
            self._shallow -= 1

    def _calls(self, call: Node, function: Node, parameters: list[str]) -> bool | None:
        """Whether a call of the function's name is a call of this function: True,
        False, or None when only the shape of the call fits."""
        owner, receiver = function._parent, getattr(call.func, "value", None)
        if isinstance(owner, ast.Module):
            return _names_function(call, function)
        selfcall = isinstance(receiver, ast.Name) and receiver.id in ("self", "cls")
        if selfcall or not isinstance(owner, ast.ClassDef):
            return owner in _ancestors(call)
        if len(self.defs[function.name]) == 1 or function.name == "__init__":
            return True
        # Several classes define the name (`set`, `delete`): the call must hand
        # the database over exactly when the method takes it first. A receiver
        # named for key/value state settles it, otherwise only the shape fits.
        first = parameters[1] if len(parameters) > 1 else ""
        handed = bool(call.args) and getattr(call.args[0], "id", "") in DB_NAMES
        if (first in DB_NAMES) != handed:
            return False
        tail = ast.unparse(receiver).rsplit(".", 1)[-1] if receiver else ""
        named = re.search(r"key_?value|kv", tail, re.IGNORECASE) or (
            tail.strip("_") == "repository"
            and "KeyValueRepository" in _module(call).source
        )
        return True if named else None

    def _callbacks(self, function: Node, name: str) -> list[tuple[Node, Node]]:
        """The calls of a function that is handed on as a callback, by its parameter."""
        owner = function._parent
        offset = int(isinstance(owner, ast.ClassDef))
        positional = [a.arg for a in function.args.args]
        if name not in positional:
            return []
        wanted = positional.index(name) - offset
        references = (
            [function]
            if isinstance(function, ast.Lambda)
            else self.passed.get(function.name, [])
        )
        return [p for ref in references for p in self._calls_of_callback(ref, wanted)]

    def _calls_of_callback(self, ref: Node, wanted: int) -> list[tuple[Node, Node]]:
        holder = ref._parent
        keyword = holder.arg if isinstance(holder, ast.keyword) else None
        call = holder._parent if keyword else holder
        if not isinstance(call, ast.Call):
            return []
        out = []
        for function in self.defs.get(_callee_name(call) or "", ()):
            owner = function._parent
            at = call.args.index(ref) if keyword is None else -1
            parameter = _parameter_at(function, at, keyword)
            if parameter is None:
                continue
            # The parameter is called directly or through the attribute it is stored in.
            stored = {
                t.attr
                for n in ast.walk(function)
                if isinstance(n, ast.Assign) and getattr(n.value, "id", "") == parameter
                for t in n.targets
                if isinstance(t, ast.Attribute)
            }
            scope = owner if isinstance(owner, ast.ClassDef) else function
            out += [
                (n, n.args[wanted])
                for n in ast.walk(scope)
                if isinstance(n, ast.Call)
                and wanted < len(n.args)
                and (
                    getattr(n.func, "id", None) == parameter
                    or getattr(n.func, "attr", None) in stored
                )
            ]
        return out


def _parameter_at(function: Node, position: int, keyword: str | None) -> str | None:
    """The parameter a callback reference lands on, by position or keyword."""
    offset = int(isinstance(function._parent, ast.ClassDef))
    positional = [a.arg for a in function.args.args]
    named = [*positional, *(a.arg for a in function.args.kwonlyargs)]
    if keyword is None and position + offset < len(positional):
        return positional[position + offset]
    return keyword if keyword in named else None


def _stores_into(node: Node, name: str) -> bool:
    """Whether a node is the target `name[key] = value`."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Store)
        and getattr(node.value, "id", None) == name
    )


def _names_function(call: Node, function: Node) -> bool:
    """Whether the callee of a call is the module-level function (by import)."""
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
    # An omitted argument keeps the parameter's default.
    if name in parameters:
        at = index - (len(parameters) - offset - len(args.defaults))
        return (function, args.defaults[at]) if at >= 0 else None
    kwonly = [a.arg for a in args.kwonlyargs]
    default = args.kw_defaults[kwonly.index(name)] if name in kwonly else None
    return None if default is None else (function, default)


@cache
def real_scanner() -> Scanner:
    return Scanner(backend_modules())


# -- the catalogue ------------------------------------------------------------


def _export_rules(scanner: Scanner) -> tuple[set[str], tuple[str, ...]]:
    """The keys and key suffixes the privacy export leaves out."""
    export = next(m for m in scanner.modules if m.name == "backend.backup.export")
    nodes: list[Node] = list(ast.walk(export.tree))
    function = next(
        n
        for n in nodes
        if isinstance(n, ast.FunctionDef) and n.name == "application_state"
    )
    names = [a.arg for a in function.args.kwonlyargs]
    default = function.args.kw_defaults[names.index("excluded_suffixes")]
    keyword: Node = next(
        n for n in nodes if isinstance(n, ast.keyword) and n.arg == "excluded_keys"
    )
    excluded: set[str] = set()
    for element in keyword.value.elts:
        starred = isinstance(element, ast.Starred)
        for v in scanner.ev(element.value if starred else element):
            items: Any = v.value
            excluded |= set(items) if starred else {str(items)}
    suffixes: Any = ast.literal_eval(default)  # type: ignore[arg-type]
    return excluded, tuple(suffixes)


def rules(scanner: Scanner | None) -> dict:
    """What the export, startup and the purge do to keys, read from their code."""
    if scanner is None:
        return {"excluded": set(), "suffixes": (), "startup": set(), "purge": set()}
    excluded, suffixes = _export_rules(scanner)
    sets = [s for s in scanner.sites if s.kind == "set"]
    bulk = [s for s in scanner.sites if s.kind == "bulk"]
    return {
        "excluded": excluded,
        "suffixes": suffixes,
        "startup": {
            s.key
            for s in sets
            if s.writer == "backend/db/bootstrap.py:apply_startup_invariants"
        },
        # The keys the purge function writes after it deleted every row.
        "purge": {
            s.key
            for b in bulk
            for s in sets
            if s.writer == b.writer and s.line > b.line
        },
    }


def _shape(key: str) -> str:
    """One row per key shape: placeholder names differ between call sites."""
    return re.sub(r"<[^>]*>", "<value>", key)


def catalogue(scanner: Scanner, rule: dict) -> str:
    by_key: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for site in scanner.sites:
        if site.kind in ("set", "delete"):
            by_key[_shape(site.key)][site.kind].add(site.writer)
    prefixes = {s.key: s.writer for s in scanner.sites if s.kind == "delete_prefix"}
    rows = []
    for key in sorted(by_key):
        removed = by_key[key]["delete"] | {
            w for p, w in prefixes.items() if key.startswith(p)
        }
        out = key in rule["excluded"] or key.endswith(rule["suffixes"])
        cells = [
            "<br>".join(sorted(by_key[key]["set"])) or "-",
            "<br>".join(sorted(removed)) or "-",
            "no" if out else "yes",
            "yes" if key in rule["startup"] else "-",
            "rewritten" if key in rule["purge"] else "-",
        ]
        rows.append(f"| `{key}` | " + " | ".join(cells) + " |")
    patterns = sum("<" in k for k in by_key)
    notes = [
        f"- every key is deleted by `{s.writer}`"
        for s in scanner.sites
        if s.kind == "bulk"
    ]
    notes += [f"- `{p}` is deleted by prefix by `{w}`" for p, w in prefixes.items()]
    notes += [
        f"- `{k}` is left out of the export; nothing writes it"
        for k in sorted(rule["excluded"] - set(by_key))
    ]
    lines = [
        f"{len(by_key) - patterns} keys and {patterns} key patterns; "
        + "`<value>` stands for a value the code computes.",
        "",
        "| Key | Written by | Deleted by | Export | Reset at start | Purge |",
        "| --- | --- | --- | --- | --- | --- |",
        *rows,
        "",
        *notes,
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


INSERT = '"INSERT INTO kv(key,value,updated_at) VALUES(?,?,?)"'
SYNTHETIC_SAVE = f"""
KEY = "typed_any_key"
class Store:
    def __init__(self, repository):
        self._repository = repository
    def save(self, db, day):
        self._repository.set(db, KEY, "v")
        self._repository.set(db, "day:" + day, "v")
        self._repository.delete(db, "removed_key")
def write(db, key):
    db.execute({INSERT}, (key, "v", "t"))
def save(db):
    write(db, "brand_new_key")
"""
SYNTHETIC_UNREADABLE = f"""
def save(db):
    db.execute({INSERT}, (compute_key(), "v", "t"))
"""
SYNTHETIC_REPOSITORY = f"""
class Profile:
    def set(self, db, payload):
        pass
class KeyValueRepository:
    def set(self, db, key, value):
        db.execute({INSERT}, (key, value, "t"))
    def delete(self, db, key):
        db.execute("DELETE FROM kv WHERE key = ?", (key,))
"""


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
        self.assertEqual(list(scanner.failures), [])

    def test_scanner_rejects_a_key_it_cannot_resolve(self):
        scanner = Scanner([synthetic_module(SYNTHETIC_UNREADABLE)])

        self.assertEqual([f[1] for f in scanner.failures], ["save"])

    def test_scanner_lists_keys_of_a_repository_of_any_name(self):
        scanner = Scanner([synthetic_module(SYNTHETIC_REPOSITORY + SYNTHETIC_SAVE)])

        self.assertEqual(list(scanner.failures), [])
        document = catalogue(scanner, rules(None))
        for key, writer in (
            ("typed_any_key", "backend/synthetic.py:Store.save"),
            ("day:<value>", "backend/synthetic.py:Store.save"),
            ("brand_new_key", "backend/synthetic.py:save"),
        ):
            self.assertIn(f"| `{key}` | {writer} |", document)
        self.assertIn(
            "| `removed_key` | - | backend/synthetic.py:Store.save |", document
        )


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
