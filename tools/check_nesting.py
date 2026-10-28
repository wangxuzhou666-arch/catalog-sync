"""Fail when a statement sits inside MAX_DEPTH or more nested control structures.

CODING-STYLE P1.1: depth, not line count, is the signal that a function is too hard to
hold in your head. Counted constructs: if / for / while / try / with / match. Not counted:
`elif` (it continues its `if`, it does not nest inside it), comprehensions, and the bodies
of def/class (each starts again at depth 0, so extracting a function is always a fix).
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_DEPTH = 5

_SKIPPED_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules", "build", "dist"})


@dataclass(frozen=True)
class Violation:
    line: int
    depth: int


def _is_elif(parent: ast.If) -> bool:
    # `elif` and `else:` + indented `if` both parse to orelse == [If]; only the column differs.
    only = parent.orelse[0] if len(parent.orelse) == 1 else None
    return isinstance(only, ast.If) and only.col_offset == parent.col_offset


def _nested_blocks(stmt: ast.stmt) -> Iterator[Sequence[ast.stmt]]:
    """Yield each child block that is one level deeper than `stmt`."""
    if isinstance(stmt, ast.If):
        yield stmt.body
        if not _is_elif(stmt):
            yield stmt.orelse
    elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        yield stmt.body
        yield stmt.orelse
    elif isinstance(stmt, (ast.Try, ast.TryStar)):
        yield stmt.body
        for handler in stmt.handlers:
            yield handler.body
        yield stmt.orelse
        yield stmt.finalbody
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        yield stmt.body
    elif isinstance(stmt, ast.Match):
        for case in stmt.cases:
            yield case.body


def _walk_block(stmts: Sequence[ast.stmt], depth: int, found: list[Violation]) -> None:
    for stmt in stmts:
        if depth >= MAX_DEPTH:
            # One report per offending block: the statements below it are the same problem.
            found.append(Violation(stmt.lineno, depth))
            return
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _walk_block(stmt.body, 0, found)
            continue
        if isinstance(stmt, ast.If) and _is_elif(stmt):
            _walk_block(stmt.orelse, depth, found)
        for block in _nested_blocks(stmt):
            _walk_block(block, depth + 1, found)


def find_violations(source: str, filename: str = "<string>") -> list[Violation]:
    found: list[Violation] = []
    _walk_block(ast.parse(source, filename=filename).body, 0, found)
    return found


def _python_files(paths: Sequence[Path]) -> Iterator[Path]:
    for path in paths:
        if path.is_file():
            yield path
            continue
        for candidate in sorted(path.rglob("*.py")):
            if not _SKIPPED_DIRS.intersection(candidate.parts):
                yield candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to check")
    args = parser.parse_args(argv)

    exit_code = 0
    for path in _python_files(args.paths):
        try:
            violations = find_violations(path.read_text(encoding="utf-8"), str(path))
        except SyntaxError as exc:
            print(f"{path}:{exc.lineno}: cannot parse: {exc.msg}", file=sys.stderr)
            exit_code = 2
            continue
        for violation in violations:
            print(
                f"{path}:{violation.line}: nesting depth {violation.depth} "
                f"(limit is {MAX_DEPTH - 1}); use guard clauses or extract a function",
                file=sys.stderr,
            )
            exit_code = max(exit_code, 1)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
