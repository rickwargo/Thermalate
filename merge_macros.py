#!/usr/bin/env python3
"""Merge Gerber aperture macros from one file into another and write a new Gerber file."""

from __future__ import annotations
import argparse
import pathlib
import sys
from typing import List


def extract_macro_blocks(src_text: str) -> List[str]:
    lines = src_text.splitlines()
    blocks: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\n")
        if line.startswith("%AM") and line.endswith("*"):
            block = [line]
            i += 1
            while i < len(lines):
                block.append(lines[i].rstrip("\n"))
                if lines[i].rstrip("\n").endswith("*%"):
                    break
                i += 1
            blocks.append("\n".join(block))
            i += 1
            continue
        i += 1
    return blocks


def merge_macros(input_path: pathlib.Path, macro_path: pathlib.Path, output_path: pathlib.Path) -> None:
    input_text = input_path.read_text(encoding="utf-8", errors="replace")
    macro_text = macro_path.read_text(encoding="utf-8", errors="replace")

    macro_blocks = extract_macro_blocks(macro_text)
    if not macro_blocks:
        raise SystemExit(f"No macro blocks found in {macro_path}")

    lines = input_text.splitlines()
    insert_at = None
    for idx, line in enumerate(lines):
        if line.strip() == "G04 Aperture macros list end*":
            insert_at = idx
            break

    if insert_at is None:
        raise SystemExit(f"Could not find 'G04 Aperture macros list end*' in {input_path}")

    insertion_lines = [
        "G04 Additional macros merged from macros.gbr*",
        *[block for block in macro_blocks],
        "G04 Additional macros merged from macros.gbr end*",
    ]

    new_lines = lines[:insert_at] + insertion_lines + lines[insert_at:]
    output_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge Gerber aperture macros into a target Gerber file")
    parser.add_argument("input", help="Source Gerber file to modify")
    parser.add_argument("macros", help="Gerber file containing macro definitions")
    parser.add_argument("output", help="Output Gerber file path")
    args = parser.parse_args()

    merge_macros(pathlib.Path(args.input), pathlib.Path(args.macros), pathlib.Path(args.output))
    print(f"Wrote merged Gerber to {args.output}")
