"""Strip local-path leaks from notebook source cells before executing on commit.

The notebooks print `config.DB_PATH` for diagnostic purposes, which leaks the
absolute Windows path (`c:/Users/Lenovo/...`) into committed outputs. Replace
with `config.DB_PATH.name` so only the filename ("real_estate.db") prints.

Run once after any source change that re-introduces the bare DB_PATH print.
"""
from __future__ import annotations

import pathlib
import sys

import nbformat

NB_DIR = pathlib.Path(__file__).resolve().parent.parent / "notebooks"

REPLACEMENTS = [
    ("config.DB_PATH)", "config.DB_PATH.name)"),
]


def sanitize_notebook(path: pathlib.Path) -> int:
    nb = nbformat.read(path, as_version=4)
    edits = 0
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        original = cell.source
        new = original
        for old, repl in REPLACEMENTS:
            if old in new and repl not in new:
                new = new.replace(old, repl)
        if new != original:
            cell.source = new
            edits += 1
    if edits:
        nbformat.write(nb, path)
    return edits


def main() -> int:
    notebooks = sorted(NB_DIR.glob("0*.ipynb"))
    if not notebooks:
        print(f"no notebooks found under {NB_DIR}", file=sys.stderr)
        return 1
    for nb in notebooks:
        n = sanitize_notebook(nb)
        print(f"{nb.name}: {n} cell(s) sanitized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
