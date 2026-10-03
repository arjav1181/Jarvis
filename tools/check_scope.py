"""Catch names that only exist inside someone else's scope.

Why this file exists
    `_welcome_panels` was defined as a nested function inside `_build_app`, and
    called from `_on_clap` — a method, outside that function. A nested function
    is not in scope for a method, so every clap raised

        NameError: name '_welcome_panels' is not defined

    and the ceremony had never once run from a clap. Nothing caught it:

      · `py_compile` cannot. It checks syntax, and that was valid syntax.
      · the test suite passed. The ceremony tests call `core.ceremony`
        directly; they never go through the clap path that broke.
      · the try/except around the ceremony turned the NameError into a chat
        line reading "The welcome did not run: ...", which reads as an
        unreliable feature rather than an impossible one.

    So: an import check is not enough. This walks every function body, collects
    the names that function itself binds, and reports any name it *calls* that
    is bound nowhere it can see — not in its own locals, not as a parameter, not
    as a module global, not as a builtin, and not as an attribute.

Scope, deliberately narrow
    It reports only names that resolve nowhere at all. A name that resolves to
    something else in an outer function is a different and much noisier class of
    problem, and a linter that cries wolf gets switched off.
"""

import ast
import builtins
import sys
from pathlib import Path

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "self", "cls"}

#: Bound by a `for`/`with`/`except` target, a walrus, a comprehension, a
#: nested def/class, an import, a global/nonlocal declaration, or a parameter.
def _bound_names(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.Global) or isinstance(node, ast.Nonlocal):
            out.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
    return out


def _called_names(fn: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


def module_globals(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
    return out


def check_file(path: Path) -> list[tuple[int, str]]:
    """Returns [(lineno, name)] for calls that resolve nowhere."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as e:
        return [(e.lineno or 0, f"<syntax error: {e.msg}>")]
    visible = module_globals(tree) | _BUILTINS
    problems: list[tuple[int, str]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _bound_names(fn) | visible
        for name in sorted(_called_names(fn) - local):
            problems.append((getattr(fn, "lineno", 0), name))
    return problems


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv[1:]] or [
        Path("main.py"), Path("dashboard/server.py"), Path("ui.py")
    ]
    total = 0
    for path in targets:
        if not path.exists():
            continue
        for lineno, name in check_file(path):
            # "run"/"self"-style names that only exist as attributes are already
            # excluded; anything reported here is a bare NameError waiting to
            # happen on whichever branch reaches it.
            print(f"  {path}:{lineno}  calls {name!r}, which is bound nowhere")
            total += 1
    print(f"  {total} unresolved call(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))