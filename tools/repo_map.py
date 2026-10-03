"""Generate docs/MAP.md: a map of the FinTrack repo for people and Claude sessions.

    python tools/repo_map.py           # (re)write docs/MAP.md
    python tools/repo_map.py --check   # exit 1 when docs/MAP.md is out of date

Standard library only (no node, no network). Python is parsed with ``ast``; TypeScript
with regular expressions, so a few unusual code shapes can be missed. The output is
deterministic: stable ordering, no timestamps, forward-slash paths, LF line endings, so
running it twice gives the same file. backend/tests/test_repo_map.py runs the check.

It reads only source folders (backend, frontend/src, frontend/scripts, packaging, tools,
SPEC.md). It never reads data folders, design/ handoffs, backups or secrets files.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_REL = "docs/MAP.md"
STALE_MESSAGE = "docs/MAP.md is out of date: run python tools/repo_map.py"

BACKEND_APP = ROOT / "backend" / "app"
ROUTERS = BACKEND_APP / "routers"
TESTS = ROOT / "backend" / "tests"
FRONT_SRC = ROOT / "frontend" / "src"
API_TS = FRONT_SRC / "api.ts"
SPEC = ROOT / "SPEC.md"

# Never descend into these (data, builds, dependencies, local-only design handoffs).
SKIP_DIRS = {
    "node_modules", "dist", "dist-release", ".venv", "venv", "__pycache__", ".pytest_cache",
    "data", "design", ".git", ".claude", "docs",
}
SOURCE_EXTS = {".py", ".pyw", ".ts", ".tsx", ".css", ".ps1"}
HTTP_METHODS = ("get", "post", "put", "patch", "delete")
# Files that talk about the marker words themselves.
SELF_FILES = {"tools/repo_map.py", "backend/tests/test_repo_map.py"}
MARKER_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b")

# Friendly titles and SPEC.md heading keywords per router module (lowercase substrings).
FEATURES: dict[str, tuple[str, tuple[str, ...]]] = {
    "accounts": ("Accounts", ("account",)),
    "alerts": ("Alerts", ("alert",)),
    "app": ("App window, launcher and health", ("app window", "launcher", "packaged", "presence", "close = lock")),
    "auth": ("Auth, vault and password", ("auth", "security model", "vault flows", "wrong-try")),
    "backup": ("Backup and restore (manual + automatic)", ("backup", "restore")),
    "budgets": ("Budget (Spending page)", ("budget", "spending", "envelope", "assign", "income")),
    "categories": ("Categories", ("categor",)),
    "category_groups": ("Category groups", ("category group",)),
    "goals": ("Goals", ("goal",)),
    "holdings": ("Holdings (Investments page)", ("holding", "investment")),
    "dashboard": ("Home v2 (GET /api/dashboard)", ("home v2", "home \"today\"")),
    "home": ("Home \"Not now\" dismissals", ("home",)),
    "loans": ("Loans and debt planner", ("loan", "debt")),
    "plaid": ("Plaid linking and sync", ("plaid", "sync")),
    "preferences": ("Preferences (Settings: auto-lock, paychecks)", ("preference", "paycheck", "settings redesign")),
    "plaid_keys": ("Plaid keys (Settings > Bank connection)", ("bank connection", "keys", "precedence")),
    "recovery": ("Recovery sheet", ("recovery", "the code", "crypto", "keyfile")),
    "recurring": ("Recurring items and cash forecast", ("recurring", "forecast", "calendar", "cadence", "occurrence")),
    "reports": ("Reports", ("report",)),
    "rules": ("Rules", ("rule",)),
    "summary": ("Summary, net worth and balance history", ("summary", "net worth", "balance history")),
    "tags": ("Tags", ("tag",)),
    "transactions": ("Transactions", ("transaction", "split", "recategorize", "needs a category", "shared filters")),
    "update": ("Private updates", ("update",)),
}

LIMIT_FILES = 12      # frontend files / tests / mocks listed per feature
LIMIT_SPEC = 8        # SPEC headings per feature
LIMIT_MARKERS = 40
# Hotspot size buckets: (min lines, max lines exclusive, label).
BUCKETS = ((1500, 10**9, "1500+ lines"), (1000, 1500, "1000-1499 lines"))
DESC_MAX = 110


# ---------------------------------------------------------------- helpers

def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def walk(base: Path, exts: set[str]) -> list[Path]:
    out: list[Path] = []
    if not base.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix in exts and not name.startswith(".env"):
                out.append(p)
    return sorted(out, key=rel)


def shorten(text: str, limit: int = DESC_MAX) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    end = text.rfind(". ", 30, limit)  # prefer a whole first sentence
    if end > 0:
        return text[: end + 1]
    cut = text[: limit - 3].rsplit(" ", 1)[0]
    return cut + "..."


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def norm_route(path: str) -> str:
    """Backend or frontend path with every parameter as ``{}`` and no query string."""
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", path)


def route_regex(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[^}]*\})", path)
    body = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts if p)
    return re.compile("^" + body + "$")


def join_list(items: list[str], limit: int, sep: str = ", ") -> str:
    if not items:
        return "none"
    shown = items[:limit]
    more = len(items) - len(shown)
    return sep.join(shown) + (f"{sep}+{more} more" if more > 0 else "")


# ---------------------------------------------------------------- Python parsing

@dataclass
class PyModule:
    path: Path
    tree: ast.Module
    text: str

    @property
    def dotted(self) -> str:
        parts = list(self.path.relative_to(ROOT / "backend").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)


_PY_CACHE: dict[Path, PyModule] = {}


def py_module(path: Path) -> PyModule:
    if path not in _PY_CACHE:
        text = read(path)
        _PY_CACHE[path] = PyModule(path, ast.parse(text, filename=str(path)), text)
    return _PY_CACHE[path]


def resolve_from(mod: PyModule, node: ast.ImportFrom) -> str | None:
    """Absolute dotted module for an ImportFrom inside backend/ (``app.x.y``)."""
    if node.level == 0:
        return node.module
    pkg = mod.dotted.split(".")
    if mod.path.name != "__init__.py":
        pkg = pkg[:-1]
    if node.level > 1:
        pkg = pkg[: len(pkg) - (node.level - 1)]
    return ".".join(pkg + ([node.module] if node.module else []))


def app_imports(mod: PyModule) -> tuple[set[str], dict[str, str]]:
    """(app modules imported, {imported name: 'module.name'}) for top-level imports."""
    modules: set[str] = set()
    names: dict[str, str] = {}
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.ImportFrom):
            base = resolve_from(mod, node)
            if not base or not (base == "app" or base.startswith("app.")):
                continue
            for alias in node.names:
                candidate = f"{base}.{alias.name}"
                if module_file(candidate) is not None:
                    modules.add(candidate)
                else:
                    modules.add(base)
                    names[alias.asname or alias.name] = candidate
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app."):
                    modules.add(alias.name)
    return modules, names


def module_file(dotted: str) -> Path | None:
    base = ROOT / "backend" / Path(*dotted.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def module_constants(mod: PyModule) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in mod.tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value.value
    return out


def docstring_line(mod: PyModule) -> str:
    doc = ast.get_docstring(mod.tree)
    if doc:
        first = doc.strip().split("\n\n", 1)[0]
        return shorten(first)
    for line in mod.text.split("\n"):
        s = line.strip()
        if s.startswith("#") and not s.startswith("#!") and "noqa" not in s:
            return shorten(s.lstrip("# "))
        if s and not s.startswith("#"):
            break
    public = [
        n.name for n in mod.tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.name.startswith("_")
    ]
    if public:
        return "(no docstring) defines " + join_list(public, 5)
    return "(no docstring)"


# ---------------------------------------------------------------- routes

@dataclass
class Route:
    method: str
    path: str
    func: str
    file: str
    line: int
    feature: str

    @property
    def key(self) -> tuple[str, str]:
        return self.method, norm_route(self.path)


@dataclass
class Feature:
    name: str
    title: str
    file: str
    routes: list[Route] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    other_modules: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    service_models: list[str] = field(default_factory=list)


def router_prefix(mod: PyModule) -> str:
    for node in mod.tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == "APIRouter":
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        return str(kw.value.value)
    return ""


def resolve_str(expr: ast.expr, mod: PyModule, consts: dict[str, str], names: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        if expr.id in consts:
            return consts[expr.id]
        target = names.get(expr.id)
        if target:
            modname, _, attr = target.rpartition(".")
            f = module_file(modname)
            if f is not None:
                return module_constants(py_module(f)).get(attr)
    return None


def parse_features() -> tuple[list[Feature], dict[str, str]]:
    """Features from backend/app/routers, plus {model class: table}."""
    models = parse_models()
    class_to_table = {m.name: m.table for m in models}
    features: list[Feature] = []
    for path in sorted(ROUTERS.glob("*.py"), key=lambda p: p.name):
        if path.name == "__init__.py":
            continue
        name = path.stem
        mod = py_module(path)
        title = FEATURES.get(name, (name.replace("_", " ").title(), ()))[0]
        feat = Feature(name, title, rel(path))
        prefix = router_prefix(mod)
        consts = module_constants(mod)
        imported, names = app_imports(mod)
        for node in mod.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if not (isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
                    continue
                method = dec.func.attr
                if method not in HTTP_METHODS or not dec.args:
                    continue
                sub = resolve_str(dec.args[0], mod, consts, names)
                if sub is None:
                    sub = "<dynamic>"
                feat.routes.append(Route(method.upper(), prefix + sub, node.name, rel(path), node.lineno, name))
        services, others, model_names = set(), set(), set()
        for m in imported:
            if m.startswith("app.services.") or m.startswith("app.updates"):
                services.add(m[len("app."):])
            elif m == "app.models":
                pass
            elif m.startswith("app.routers"):
                others.add(m[len("app."):])
            else:
                others.add(m[len("app."):])
        for local, target in names.items():
            modname, _, attr = target.rpartition(".")
            if modname == "app.models" and attr in class_to_table:
                model_names.add(attr)
        via: set[str] = set()
        for s in services:
            f = module_file("app." + s)
            if f is None:
                continue
            smod = py_module(f)
            _, snames = app_imports(smod)
            for target in snames.values():
                modname, _, attr = target.rpartition(".")
                if modname == "app.models" and attr in class_to_table and attr not in model_names:
                    via.add(attr)
        feat.services = sorted(services)
        feat.other_modules = sorted(o for o in others if o not in ("deps", "schemas", "utils"))
        feat.models = sorted(model_names)
        feat.service_models = sorted(via)
        features.append(feat)
    return features, class_to_table


# ---------------------------------------------------------------- models / migrations

@dataclass
class Model:
    name: str
    table: str
    line: int
    columns: list[str]


def _col_type(ann: ast.expr | None, value: ast.expr) -> str:
    fk = ""
    base = ""
    if isinstance(value, ast.Call):
        for arg in value.args:
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "ForeignKey":
                if arg.args and isinstance(arg.args[0], ast.Constant):
                    fk = f" -> {str(arg.args[0].value).split('.')[0]}"
            elif isinstance(arg, ast.Name) and not base:
                base = arg.id
    if ann is not None:
        inner = ann
        if isinstance(ann, ast.Subscript):
            inner = ann.slice
        text = ast.unparse(inner).replace("dt.", "")
        text = text.replace(" | None", "?")
        return text + fk
    return (base or "?") + fk


def parse_models() -> list[Model]:
    mod = py_module(BACKEND_APP / "models.py")
    out: list[Model] = []
    for node in mod.tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        table = None
        cols: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for t in stmt.targets:
                    if isinstance(t, ast.Name) and t.id == "__tablename__" and isinstance(stmt.value, ast.Constant):
                        table = str(stmt.value.value)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                v = stmt.value
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "mapped_column":
                    cols.append(f"{stmt.target.id}: {_col_type(stmt.annotation, v)}")
        if table:
            out.append(Model(node.name, table, node.lineno, cols))
    return out


def migrations_latest() -> tuple[str, int, list[str]]:
    mod = py_module(BACKEND_APP / "migrations.py")
    latest, line = "?", 0
    for node in mod.tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LATEST" for t in node.targets):
            latest, line = ast.unparse(node.value), node.lineno
    steps = [
        f"`{n.name}`" for n in mod.tree.body
        if isinstance(n, ast.FunctionDef) and re.fullmatch(r"_v\d+", n.name)
    ]
    return latest, line, steps


# ---------------------------------------------------------------- api.ts

@dataclass
class ApiFunc:
    name: str        # dotted, without the leading "api."
    line: int
    calls: list[tuple[str, str]]  # (METHOD, normalized path)


# request<T>('GET', '/api/x' | "/api/x" | `/api/x/${id}${qs({ a: 'b' })}`, ...)
_CALL_RE = re.compile(
    r"""['"](GET|POST|PUT|PATCH|DELETE)['"]\s*,\s*(?:'(/api[^']*)'|"(/api[^"]*)"|`(/api[^`]*)`)"""
)


def _norm_front(path: str) -> str:
    path = re.sub(r"\$\{qs\(.*?\)\}", "", path, flags=re.S)
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    return norm_route(path)


def strip_ts_comments(lines: list[str]) -> list[str]:
    out: list[str] = []
    in_block = False
    for raw in lines:
        line = raw
        if in_block:
            end = line.find("*/")
            if end < 0:
                out.append("")
                continue
            line = line[end + 2:]
            in_block = False
        while True:
            start = line.find("/*")
            if start < 0:
                break
            end = line.find("*/", start + 2)
            if end < 0:
                line = line[:start]
                in_block = True
                break
            line = line[:start] + line[end + 2:]
        line = re.sub(r"(^|\s)//.*$", "", line)
        out.append(line)
    return out


def parse_api_ts() -> list[ApiFunc]:
    if not API_TS.is_file():
        return []
    lines = strip_ts_comments(read(API_TS).split("\n"))
    start = next((i for i, l in enumerate(lines) if re.match(r"^export const api\s*=\s*\{", l)), None)
    if start is None:
        return []
    funcs: dict[str, ApiFunc] = {}
    texts: dict[str, list[str]] = defaultdict(list)
    depth, stack, cur = 1, [], None
    for i in range(start + 1, len(lines)):
        code = lines[i]
        if depth == len(stack) + 1:
            m = re.match(r"^\s*([A-Za-z_]\w*)\s*:\s*(.*)$", code)
            if m:
                key, rest = m.groups()
                if rest.strip() == "{":
                    stack.append(key)
                    cur = None
                else:
                    cur = ".".join(stack + [key])
                    funcs[cur] = ApiFunc(cur, i + 1, [])
        if cur:
            texts[cur].append(code)
        depth += code.count("{") - code.count("}")
        if depth <= 0:
            break
        while stack and depth < len(stack) + 1:
            stack.pop()
            cur = None
    for name, f in funcs.items():
        joined = "\n".join(texts[name])
        f.calls = [
            (m.group(1), _norm_front(m.group(2) or m.group(3) or m.group(4))) for m in _CALL_RE.finditer(joined)
        ]
    return sorted(funcs.values(), key=lambda f: f.line)


# ---------------------------------------------------------------- frontend files

def frontend_files() -> list[Path]:
    files = walk(FRONT_SRC, {".ts", ".tsx", ".css"})
    return [p for p in files if not re.search(r"\.(test|spec)\.tsx?$", p.name)]


def is_mock(path: Path) -> bool:
    return rel(path).startswith("frontend/src/dev/")


# `api.loans\n  .payoff(...)` (a call split over lines) counts too.
_API_REF = re.compile(r"\bapi\s*\.\s*([A-Za-z_]\w*)(?:\s*\.\s*([A-Za-z_]\w*))?")


def api_refs(files: list[Path], funcs: list[ApiFunc]) -> dict[str, set[str]]:
    """{api function name: files (not api.ts, not dev mocks) that reference it}."""
    known = {f.name for f in funcs}
    out: dict[str, set[str]] = defaultdict(set)
    for p in files:
        if p == API_TS or is_mock(p) or p.suffix == ".css":
            continue
        text = "\n".join(strip_ts_comments(read(p).split("\n")))
        for m in _API_REF.finditer(text):
            ref = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
            if ref in known:
                out[ref].add(rel(p))
            else:
                head = ref.split(".")[0]
                if head in known:
                    out[head].add(rel(p))
    return out


_MOCK_PATH = re.compile(r"/api/[A-Za-z0-9_\-/{}.$]*")


def mock_paths(path: Path) -> set[str]:
    text = read(path).replace("\\/", "/").replace("[/]", "/")
    found = set()
    for m in _MOCK_PATH.finditer(text):
        p = re.sub(r"\$\{[^}]*\}", "{}", m.group(0)).rstrip(".")
        found.add(p)
    return found


@dataclass
class _Comment:
    start: int  # 0-based first line
    end: int    # 0-based last line
    first: str  # first meaningful line ('' when none)


def _first_paragraph(texts: list[str], skip: tuple[str, ...]) -> str:
    para: list[str] = []
    for t in texts:
        if not t or t.startswith(skip):
            if para:
                break
            continue
        para.append(t)
    return " ".join(para)


def _comment_blocks(lines: list[str]) -> list[_Comment]:
    """Comment blocks that start a line: /* ... */ blocks and runs of // lines."""
    out: list[_Comment] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("/*"):
            start, body = i, []
            chunk = s[2:]
            while True:
                end = chunk.find("*/")
                if end >= 0:
                    body.append(chunk[:end])
                    break
                body.append(chunk)
                i += 1
                if i >= len(lines):
                    break
                chunk = lines[i].strip()
            texts = [b.strip().lstrip("*").strip() for b in body]
            first = _first_paragraph(texts, ("@", "eslint"))
            out.append(_Comment(start, min(i, len(lines) - 1), first))
        elif s.startswith("//"):
            start, texts = i, []
            while i < len(lines) and lines[i].strip().startswith("//"):
                texts.append(lines[i].strip().lstrip("/").strip())
                i += 1
            i -= 1
            first = _first_paragraph(texts, ("@ts-", "eslint", "---"))
            out.append(_Comment(start, i, first))
        i += 1
    return out


def first_ts_comment(text: str, stem: str) -> str | None:
    """The file's header comment, else the JSDoc on its main export, else None.

    A header is a comment before any code other than imports that is not glued to the
    line after it (a blank line or an import follows), or one glued to an ``export``.
    """
    lines = text.split("\n")
    blocks = _comment_blocks(lines)
    by_start = {b.start: b for b in blocks}
    by_end = {b.end: b for b in blocks}
    in_import = False
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if i in by_start:
            b = by_start[i]
            nxt = b.end + 1
            after = lines[nxt].strip() if nxt < len(lines) else ""
            if b.first and (not after or after.startswith(("import ", "/*", "//")) or after.startswith("export ")):
                return shorten(b.first)
            i = b.end + 1
            continue
        if in_import:
            if re.search(r"from\s+['\"][^'\"]+['\"];?\s*$", s) or s.endswith("';") or s.endswith('";'):
                in_import = False
        elif s.startswith("import "):
            in_import = not re.search(r"(from\s+)?['\"][^'\"]+['\"];?\s*$", s)
        elif s:
            break  # first real code: no header
        i += 1
    exports = [
        (n, m.group(1)) for n, line in enumerate(lines)
        if (m := re.match(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class)\s+(\w+)", line))
    ]
    exports.sort(key=lambda e: (e[1] != stem, e[0]))
    for n, _ in exports:
        b = by_end.get(n - 1)
        if b and b.first:
            return shorten(b.first)
    return None


def ts_exports(text: str) -> list[str]:
    names = re.findall(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|interface|type)\s+(\w+)", text, re.M)
    return names


# ---------------------------------------------------------------- tests

@dataclass
class TestFile:
    path: Path
    count: int
    imports: list[str]
    paths: list[str]


def test_count(tree: ast.Module) -> int:
    n = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            n += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            n += sum(
                1 for s in node.body
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name.startswith("test")
            )
    return n


def string_paths(tree: ast.Module) -> list[str]:
    out: list[str] = []
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                inside_fstring.add(id(v))
                if isinstance(v, ast.Constant):
                    parts.append(str(v.value))
                else:
                    parts.append("{}")
            s = "".join(parts)
            if s.startswith("/api"):
                out.append(s.split("?", 1)[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inside_fstring:
            if node.value.startswith("/api"):
                out.append(node.value.split("?", 1)[0])
    return out


def parse_tests() -> list[TestFile]:
    out: list[TestFile] = []
    for path in sorted(TESTS.glob("*.py"), key=lambda p: p.name):
        mod = py_module(path)
        imported: set[str] = set()
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                if node.module in ("app.routers", "app.services", "app.updates"):
                    for a in node.names:
                        imported.add(f"{node.module[4:]}.{a.name}")
                elif node.module.startswith(("app.routers.", "app.services.", "app.updates.")):
                    imported.add(node.module[4:])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith(("app.routers.", "app.services.", "app.updates.")):
                        imported.add(a.name[4:])
        out.append(TestFile(path, test_count(mod.tree), sorted(imported), string_paths(mod.tree)))
    return out


# ---------------------------------------------------------------- SPEC.md

@dataclass
class Heading:
    level: int
    text: str
    line: int
    crumb: str
    body: str


def parse_spec() -> list[Heading]:
    if not SPEC.is_file():
        return []
    lines = read(SPEC).split("\n")
    heads: list[Heading] = []
    parents: dict[int, str] = {}
    in_code = False
    idx: list[int] = []
    for i, line in enumerate(lines):
        if line.startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            parents = {k: v for k, v in parents.items() if k < level}
            crumb = " > ".join([parents[k] for k in sorted(parents)] + [text])
            parents[level] = text
            heads.append(Heading(level, text, i + 1, crumb, ""))
            idx.append(i)
    for n, h in enumerate(heads):
        end = idx[n + 1] if n + 1 < len(idx) else len(lines)
        h.body = "\n".join(lines[idx[n] + 1: end])
    return heads


def spec_for(feature: Feature, heads: list[Heading]) -> list[str]:
    keywords = FEATURES.get(feature.name, ("", (feature.name.replace("_", " "),)))[1]
    stems = sorted({re.split(r"/\{", r.path)[0].rstrip("/") for r in feature.routes if r.path.startswith("/api")})
    found: list[Heading] = []
    for h in heads:
        low = h.text.lower()
        by_title = any(k in low for k in keywords)
        refs = 0
        for stem in stems:
            refs += len(re.findall(re.escape(stem) + r"(?![\w-])", h.text + "\n" + h.body))
        if by_title or refs:
            found.append(h)  # document order, so edits elsewhere in SPEC.md don't reorder it
    out = [f"{h.crumb}" for h in found[:LIMIT_SPEC]]
    extra = len(found) - LIMIT_SPEC
    if extra > 0:
        out.append(f"+{extra} more (grep SPEC.md)")
    return out


# ---------------------------------------------------------------- render sections

def render_header() -> list[str]:
    return [
        "# Iron Owl repo map",
        "",
        "Generated by `tools/repo_map.py`; do not edit by hand. Regenerate after changing code:",
        "`python tools/repo_map.py` (check without writing: `python tools/repo_map.py --check`,",
        "which `backend/tests/test_repo_map.py` also runs). Use the backend venv's python.",
        "No line numbers or sizes on purpose: ordinary edits keep the map current; renames, moves and",
        "new or removed files, routes, functions, tables or tests make it stale. Search for the names",
        "given here. The hand-written guides are in `docs/guide/`; the product contract is `SPEC.md`.",
        "",
        "Contents: [Features](#features-index) | [Frontend routes](#frontend-routes) |"
        " [Backend layout](#backend-layout) | [Frontend layout](#frontend-layout) |"
        " [Database](#database) | [Tests](#tests) | [Hotspots](#hotspots) | [Drift](#api-drift)",
        "",
    ]


def render_features(features: list[Feature], funcs: list[ApiFunc], refs: dict[str, set[str]],
                    tests: list[TestFile], heads: list[Heading], mocks: dict[str, set[str]],
                    class_to_table: dict[str, str]) -> list[str]:
    out = ["## Features index", ""]
    out.append("One section per router in `backend/app/routers/`. Flow: page -> `api.ts` function -> route ->"
               " services -> models. \"Used by\" = files in `frontend/src` (not dev mocks) that call the api.ts"
               " functions; \"Tests\" = backend test files whose `/api/...` strings hit these routes (files named"
               " after the feature first). SPEC.md headings are in document order (search for the text).")
    out.append("")
    out.append("| Feature | Router | Routes | Page / main UI |")
    out.append("|---|---|---|---|")
    all_routes = [r for f in features for r in f.routes]
    by_key: dict[tuple[str, str], list[ApiFunc]] = defaultdict(list)
    for fn in funcs:
        for call in dict.fromkeys(fn.calls):
            by_key[call].append(fn)
    feat_ui: dict[str, list[str]] = {}
    feat_main: dict[str, str] = {}
    for f in features:
        weight: dict[str, int] = defaultdict(int)
        fnames = sorted({fn.name for r in f.routes for fn in by_key.get(r.key, [])})
        for name in fnames:
            for file in refs.get(name, set()):
                weight[file] += 1
        feat_ui[f.name] = sorted(weight)
        pages = [p for p in weight if "/pages/" in p] or list(weight)
        best = min(pages, key=lambda p: (-weight[p], p)) if pages else "-"
        feat_main[f.name] = best.split("/pages/", 1)[1] if "/pages/" in best else best.replace("frontend/src/", "")
    for f in features:
        main = feat_main[f.name]
        anchor = "feature-" + f.name.replace("_", "-")
        out.append(f"| [{cell(f.title)}](#{anchor}) | `{f.file.split('/')[-1]}` | {len(f.routes)} | `{main}` |")
    out.append("")

    route_regexes = [(r, route_regex(norm_route(r.path))) for r in all_routes]

    def hits(paths: list[str]) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for p in paths:
            p = norm_route(p)
            cands = [r for r, rx in route_regexes if rx.match(p) or (p.endswith("/") and rx.match(p + "x"))]
            if cands:
                best = min(cands, key=lambda r: (r.path.count("{"), r.path))
                counts[best.feature] += 1
        return counts

    test_hits = {t.path: hits(t.paths) for t in tests}

    for f in features:
        out.append(f'<a id="feature-{f.name.replace("_", "-")}"></a>')
        out.append(f"### {f.title} (`{f.name}`)")
        out.append("")
        out.append(f"Router `{f.file}`. Services: " + join_list([f"`{s}`" for s in f.services], 20)
                   + (". Also imports: " + join_list([f"`{o}`" for o in f.other_modules], 12) if f.other_modules else "")
                   + ".")
        tables = [f"{m} ({class_to_table[m]})" for m in f.models]
        line = "Models (router imports): " + join_list(tables, 20)
        if f.service_models:
            line += ". Via its services: " + join_list(f.service_models, 10)
        out.append("")
        out.append(line + ".")
        out.append("")
        out.append("| Method | Path | Function | api.ts |")
        out.append("|---|---|---|---|")
        for r in f.routes:
            fns = by_key.get(r.key, [])
            client = ", ".join(f"`{fn.name}`" for fn in fns) if fns else "-"
            out.append(f"| {r.method} | `{r.path}` | `{r.func}` | {client} |")
        out.append("")
        clients = sorted({fn.name: fn for r in f.routes for fn in by_key.get(r.key, [])}.values(), key=lambda x: x.line)
        if clients:
            out.append("- api.ts: " + join_list([f"`api.{c.name}`" for c in clients], 30))
        ui = feat_ui[f.name]
        out.append("- Used by: " + join_list([f"`{u.replace('frontend/src/', '')}`" for u in ui], LIMIT_FILES))
        feat_mocks = sorted(mocks.get(f.name, set()))
        out.append("- Dev mocks: " + join_list([f"`{m.replace('frontend/src/', '')}`" for m in feat_mocks], LIMIT_FILES))
        words = (f.name,) + FEATURES.get(f.name, ("", ()))[1]
        th = sorted((t.path.name for t in tests if test_hits[t.path].get(f.name)),
                    key=lambda n: (not any(w.replace(" ", "_") in n for w in words), n))
        out.append("- Tests: " + join_list([f"`{n}`" for n in th], LIMIT_FILES))
        spec = spec_for(f, heads)
        if spec:
            out.append("- SPEC.md:")
            for s in spec:
                out.append(f"  - {s}")
        else:
            out.append("- SPEC.md: none found")
        out.append("")
    return out


def render_frontend_routes() -> list[str]:
    out = ["## Frontend routes", ""]
    app = FRONT_SRC / "App.tsx"
    if not app.is_file():
        return out + ["(App.tsx not found)", ""]
    text = read(app)
    sources: dict[str, str] = {}
    for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*'([^']+)'", text):
        for name in m.group(1).split(","):
            name = name.strip().split(" as ")[-1].strip()
            if name:
                sources[name] = m.group(2)
    for m in re.finditer(r"const\s+(\w+)\s*=\s*lazyPage\(\(\)\s*=>\s*import\('([^']+)'\)", text):
        sources[m.group(1)] = m.group(2) + " (lazy)"

    for m in re.finditer(r"^function\s+(\w+)\s*\(", text, re.M):
        sources.setdefault(m.group(1), "(local)")
    for m in re.finditer(r"^function\s+(\w+)\s*\([^)]*\)\s*\{(.*?)^\}", text, re.M | re.S):
        nav = re.search(r"<Navigate\s+to=\{?[`'\"]([^`'\"$]+)", m.group(2))
        if nav:
            sources[m.group(1)] = f"(local) redirects to {nav.group(1)}"

    def src(comp: str) -> str:
        s = sources.get(comp, "?")
        if s.startswith("(local)"):
            return "`frontend/src/App.tsx`" + s[len("(local)"):]
        lazy = s.endswith(" (lazy)")
        s = s.removesuffix(" (lazy)")
        if s.startswith("./"):
            base = "frontend/src/" + s[2:]
            for ext in (".tsx", ".ts"):
                if (ROOT / (base + ext)).is_file():
                    base += ext
                    break
            s = f"`{base}`"
        else:
            s = f"`{s}`"
        return s + (" (lazy)" if lazy else "")

    out.append("HashRouter (`#/path`), defined in `frontend/src/App.tsx`, inside `<Layout />`:")
    out.append("")
    out.append("| Path | Component | File |")
    out.append("|---|---|---|")
    for m in re.finditer(r"<Route\s+(index|path=\"([^\"]*)\")\s+element=\{<(\w+)", text):
        path = "/" if m.group(1) == "index" else "/" + (m.group(2) or "")
        comp = m.group(3)
        if path == "/*":
            out.append(f"| `*` | `{comp}` | redirect to `/` |")
        else:
            out.append(f"| `{path}` | `{comp}` | {src(comp)} |")
    out.append("")
    gates = re.findall(r"if \(([^)]*)\) return <(\w+)", text)
    if gates:
        out.append("Before the app (the `Gate` in App.tsx):")
        out.append("")
        for cond, comp in gates:
            out.append(f"- `{cond.strip()}` -> `{comp}` ({src(comp)})")
        out.append("")
    layout = FRONT_SRC / "components" / "Layout.tsx"
    if layout.is_file():
        nav = re.findall(r"\{\s*to:\s*'([^']+)',\s*label:\s*'([^']+)',\s*icon:\s*'([^']+)'\s*\}", read(layout))
        out.append("Sidebar nav (`frontend/src/components/Layout.tsx`, `NAV`): "
                   + " | ".join(f"{label} `{to}`" for to, label, _ in nav))
        out.append("")
    return out


def render_backend_layout(features: list[Feature]) -> list[str]:
    by_file = {f.file: f for f in features}
    out = ["## Backend layout", ""]
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in walk(ROOT / "backend", {".py"}):
        r = rel(p)
        if r.startswith("backend/tests/"):
            continue
        groups[str(Path(r).parent.as_posix())].append(p)
    for extra in (ROOT / "packaging", ROOT / "tools"):
        for p in walk(extra, {".py", ".pyw", ".ps1"}):
            if "/tests/" in rel(p):
                continue
            groups[str(Path(rel(p)).parent.as_posix())].append(p)
    for folder in sorted(groups):
        out.append(f"**{folder}/**")
        out.append("")
        for p in groups[folder]:
            if p.suffix == ".ps1":
                desc = ps1_desc(p)
            else:
                mod = py_module(p)
                desc = docstring_line(mod)
                if folder == "backend/app/routers" and desc.startswith("(no docstring)") and p.name != "__init__.py":
                    feat = by_file.get(rel(p))
                    stems = sorted({"/".join(r.path.split("/")[:3]) for r in feat.routes}) if feat else []
                    desc = f"API router ({len(feat.routes) if feat else 0} routes): " + ", ".join(
                        f"`{s}...`" for s in stems)
            out.append(f"- `{p.name}` {desc}")
        out.append("")
    return out


def ps1_desc(path: Path) -> str:
    for line in read(path).split("\n"):
        s = line.strip()
        if s.startswith("<#"):
            s = s[2:].strip()
            if not s:
                continue
        if s.startswith("#"):
            s = s.lstrip("# ").strip()
            if s:
                return shorten(s)
            continue
        if s.startswith(".SYNOPSIS") or not s:
            continue
        if s.startswith("param") or s.startswith("$") or s.startswith("[") or s.startswith("Set-"):
            break
        return shorten(s)
    return "(no comment)"


def render_frontend_layout(files: list[Path]) -> list[str]:
    out = ["## Frontend layout", ""]
    out.append("Every file in `frontend/src` with its header comment (or its main export's doc, or its exports)."
               " Unit tests live in `frontend/scripts/*.test.ts` (`npm run test:unit`).")
    out.append("")
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in files:
        groups[Path(rel(p)).parent.as_posix()].append(p)
    for folder in sorted(groups):
        out.append(f"**{folder}/**")
        out.append("")
        css = []
        for p in groups[folder]:
            text = read(p)
            if p.suffix == ".css":
                css.append(f"`{p.name}`")
                continue
            desc = first_ts_comment(text, p.stem)
            if not desc:
                exports = ts_exports(text)
                desc = "exports " + join_list(exports, 5) if exports else "(no comment)"
            mock = " [dev mock]" if is_mock(p) else ""
            out.append(f"- `{p.name}`{mock} {desc}")
        if css:
            out.append("- styles: " + ", ".join(css))
        out.append("")
    return out


def render_database(models: list[Model]) -> list[str]:
    latest, line, steps = migrations_latest()
    out = ["## Database", ""]
    out.append(f"`backend/app/models.py` (SQLAlchemy 2.0, SQLCipher-encrypted SQLite). Schema version:"
               f" `LATEST = {latest}` in `backend/app/migrations.py`. Steps: {', '.join(steps)}."
               " Money columns are integer cents (`*_cents`). New simple values usually go in the"
               " `app_settings` key/value table instead of a migration.")
    out.append("")
    out.append("| Model | Table | Columns |")
    out.append("|---|---|---|")
    for m in models:
        out.append(f"| `{m.name}` | `{m.table}` | {cell(', '.join(m.columns))} |")
    out.append("")
    return out


def render_tests(tests: list[TestFile], features: list[Feature]) -> list[str]:
    out = ["## Tests", ""]
    total = sum(t.count for t in tests)
    out.append(f"`backend/tests/`: {len(tests)} files, {total} test functions. `conftest.py` has the"
               " fixtures (`SessionClient`, `FakePlaid`, `make_settings`). Imports = routers/services/updates"
               " modules imported directly (most tests go through the HTTP client instead).")
    out.append("")
    out.append("| File | Tests | Imports |")
    out.append("|---|---|---|")
    for t in tests:
        if t.count == 0 and not t.path.name.startswith("test_"):
            continue  # helpers (conftest.py, update_helpers.py)
        out.append(f"| `{t.path.name}` | {t.count} | {cell(join_list(t.imports, 8))} |")
    out.append("")
    other: list[str] = []
    for p in walk(ROOT / "packaging", {".py"}):
        if "/tests/" in rel(p):
            other.append(f"`{rel(p)}` ({test_count(py_module(p).tree)})")
    for p in walk(ROOT / "frontend" / "scripts", {".ts"}):
        n = len(re.findall(r"^\s*(?:test|it)\(", read(p), re.M))
        other.append(f"`{rel(p)}` ({n})")
    if other:
        out.append("Other tests: " + ", ".join(other) + ".")
        out.append("")
    return out


def render_hotspots() -> list[str]:
    out = ["## Hotspots", ""]
    files: list[Path] = []
    for base in (ROOT / "backend", FRONT_SRC, ROOT / "frontend" / "scripts", ROOT / "packaging", ROOT / "tools"):
        files += walk(base, SOURCE_EXTS)
    files = sorted(set(files), key=rel)
    markers: list[str] = []
    sizes: list[tuple[int, str]] = []
    for p in files:
        r = rel(p)
        lines = read(p).split("\n")
        sizes.append((line_count(read(p)), r))
        if r in SELF_FILES:
            continue
        for line in lines:
            m = MARKER_RE.search(line)
            if m:
                markers.append(f"- `{r}` {m.group(1)}: {cell(shorten(line.strip(), 100))}")
    out.append("### TODO / FIXME / XXX / HACK")
    out.append("")
    if markers:
        out += markers[:LIMIT_MARKERS]
        if len(markers) > LIMIT_MARKERS:
            out.append(f"- +{len(markers) - LIMIT_MARKERS} more")
    else:
        out.append("None found.")
    out.append("")
    out.append("### Largest source files")
    out.append("")
    out.append("Big files are where bugs hide. Coarse buckets (no exact sizes, so ordinary edits don't"
               " change the map):")
    out.append("")
    for low, high, label in BUCKETS:
        names = sorted(r for n, r in sizes if low <= n < high)
        out.append(f"- {label}: " + (", ".join(f"`{r}`" for r in names) if names else "none"))
    out.append("")
    return out


def render_drift(features: list[Feature], funcs: list[ApiFunc]) -> list[str]:
    out = ['<a id="api-drift"></a>', "## API drift", ""]
    backend = {r.key for f in features for r in f.routes}
    extra = sorted((fn.name, m, p) for fn in funcs for m, p in fn.calls if (m, p) not in backend)
    called = {c for fn in funcs for c in fn.calls}
    unused = sorted((r.method, r.path, r.file.split("/")[-1]) for f in features for r in f.routes if r.key not in called)
    out.append("api.ts calls with no matching backend route (should be empty):")
    out.append("")
    out += [f"- `api.{n}`: {m} `{p}`" for n, m, p in extra] or ["- none"]
    out.append("")
    out.append("Backend routes that api.ts never calls (launcher-only, file downloads, or unused):")
    out.append("")
    out += [f"- {m} `{p}` ({f})" for m, p, f in unused] or ["- none"]
    out.append("")
    return out


# ---------------------------------------------------------------- main

def generate() -> str:
    features, class_to_table = parse_features()
    models = parse_models()
    funcs = parse_api_ts()
    ffiles = frontend_files()
    refs = api_refs(ffiles, funcs)
    tests = parse_tests()
    heads = parse_spec()

    route_rx = [(f.name, route_regex(norm_route(r.path)), norm_route(r.path)) for f in features for r in f.routes]
    mocks: dict[str, set[str]] = defaultdict(set)
    for p in ffiles:
        if not is_mock(p) or p.suffix == ".css":
            continue
        for mp in mock_paths(p):
            probe = mp.rstrip("/")
            for fname, rx, rpath in route_rx:
                if rx.match(norm_route(mp)) or (probe.count("/") >= 2 and rpath.startswith(probe + "/")):
                    mocks[fname].add(rel(p))

    parts: list[str] = []
    parts += render_header()
    parts += render_features(features, funcs, refs, tests, heads, mocks, class_to_table)
    parts += render_frontend_routes()
    parts += render_backend_layout(features)
    parts += render_frontend_layout(ffiles)
    parts += render_database(models)
    parts += render_tests(tests, features)
    parts += render_hotspots()
    parts += render_drift(features, funcs)
    text = "\n".join(parts).rstrip("\n") + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


def check(content: str) -> tuple[bool, str]:
    target = ROOT / MAP_REL
    if not target.is_file():
        return False, f"{STALE_MESSAGE} (docs/MAP.md does not exist)"
    current = read(target)
    if current == content:
        return True, "docs/MAP.md is up to date"
    old, new = current.split("\n"), content.split("\n")
    for i, (a, b) in enumerate(zip(old, new), 1):
        if a != b:
            return False, f"{STALE_MESSAGE}\n  first difference at line {i}:\n  - {a[:160]}\n  + {b[:160]}"
    return False, f"{STALE_MESSAGE}\n  length differs: {len(old)} vs {len(new)} lines"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate docs/MAP.md (the Iron Owl repo map).")
    parser.add_argument("--check", action="store_true", help="exit 1 if docs/MAP.md is out of date")
    args = parser.parse_args(argv)
    content = generate()
    if args.check:
        ok, message = check(content)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    target = ROOT / MAP_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    print(f"wrote {MAP_REL} ({content.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
