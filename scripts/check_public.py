"""Check the public source set for accidental private files and broken doc links.

This is a release guard, not a guarantee that arbitrary text contains no secrets.
An operator may supply a private newline-delimited deny list with --deny-file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {
    ".py",
    ".js",
    ".cjs",
    ".json",
    ".md",
    ".toml",
    ".yml",
    ".yaml",
    ".html",
    ".css",
    ".service",
    ".lock",
}
PATTERNS = (
    (
        "private home path",
        re.compile(r"/home/(?!(?:demo|example|test-user)(?:/|$))[a-zA-Z0-9._-]+/"),
    ),
    ("private LAN address", re.compile(r"\b192\.168\.(?!0\.1\b)[0-9]{1,3}\.[0-9]{1,3}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".key", ".log", ".pem"}


def candidates(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return sorted({root / value.decode() for value in result.stdout.split(b"\0") if value})
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not any(
            part
            in {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "build"}
            for part in path.relative_to(root).parts
        )
    )


def check(root: Path, deny: tuple[str, ...] = ()) -> tuple[list[str], int]:
    errors = []
    paths = candidates(root)
    for path in paths:
        relative = path.relative_to(root)
        if path.is_symlink():
            errors.append(f"{relative}: symlink is not a public source file")
            continue
        if (
            path.suffix in FORBIDDEN_SUFFIXES
            or path.name.endswith(".local.toml")
            or path.name == "config.toml"
            or path.name == ".env"
            or path.name.startswith(".env.")
        ):
            errors.append(f"{relative}: private artifact type")
            continue
        if path.suffix not in SUFFIXES and path.name not in {"LICENSE", ".gitignore"}:
            continue
        text = path.read_text(encoding="utf-8")
        for label, pattern in PATTERNS:
            if pattern.search(text):
                errors.append(f"{relative}: {label}")
        for value in deny:
            if value and value.casefold() in text.casefold():
                # Do not echo a possibly private deny-list value into the receipt.
                errors.append(f"{relative}: private deny-list match")
        if path.suffix == ".md":
            for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                target = target.split("#", 1)[0]
                if target and not (path.parent / target).exists():
                    errors.append(f"{relative}: broken local link {target}")
    return errors, len(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deny-file", type=Path)
    args = parser.parse_args()
    deny = tuple(args.deny_file.read_text().splitlines()) if args.deny_file else ()
    errors, count = check(ROOT, deny)
    for error in errors:
        print(error)
    print(f"Public source gate: {count} files checked, {len(errors)} findings")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
