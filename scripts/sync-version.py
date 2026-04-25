#!/usr/bin/env python3
"""Mirror backend/_version.py::__version__ into frontend/package.json.

Usage:
    python scripts/sync-version.py            # write package.json if needed
    python scripts/sync-version.py --check    # exit 1 if package.json is stale
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "backend" / "_version.py"
PACKAGE_JSON = ROOT / "frontend" / "package.json"

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)


def read_backend_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        sys.stderr.write(f"error: cannot parse __version__ from {VERSION_FILE}\n")
        sys.exit(2)
    return m.group(1)


def read_package_version() -> tuple[dict, str]:
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return data, data.get("version", "")


def main() -> int:
    check_only = "--check" in sys.argv

    backend_version = read_backend_version()
    package_data, package_version = read_package_version()

    if backend_version == package_version:
        print(f"ok: both at {backend_version}")
        return 0

    if check_only:
        sys.stderr.write(
            f"version drift: backend={backend_version} package.json={package_version}\n"
            f"run `python scripts/sync-version.py` to sync\n"
        )
        return 1

    package_data["version"] = backend_version
    PACKAGE_JSON.write_text(
        json.dumps(package_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"updated package.json: {package_version} -> {backend_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
