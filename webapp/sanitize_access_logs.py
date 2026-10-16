#!/usr/bin/env python3
"""Redact credential-like fields from legacy Web access logs in place."""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from .server import redact_sensitive_text
except ImportError:  # Executed directly from webapp/.
    from server import redact_sensitive_text


def sanitize_file(path: Path) -> int:
    if not path.is_file():
        return 0
    source_mode = path.stat().st_mode
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.redacting")
    changed = 0
    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as source:
            with temp_path.open("w", encoding="utf-8", errors="surrogateescape", newline="") as target:
                for line in source:
                    redacted = redact_sensitive_text(line)
                    changed += redacted != line
                    target.write(redacted)
                target.flush()
                os.fsync(target.fileno())
        os.chmod(temp_path, source_mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return changed


def main(argv: list[str]) -> int:
    paths = [Path(value) for value in argv] if argv else [Path(__file__).with_name("webapp.log")]
    for path in paths:
        changed = sanitize_file(path)
        print(f"{path}: redacted {changed} line(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
