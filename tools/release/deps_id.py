"""Print the runtime id (``deps_id``) for backend/requirements.txt.

A packaged install keeps its Python packages in ``runtimes/<deps_id>/site-packages`` so an
app-only update can reuse them; the id changes exactly when the runtime requirements (or
the target platform) change. 16 lowercase hex characters.

    python tools/release/deps_id.py backend/requirements.txt
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

TARGET = "cp311-win_amd64"
DOMAIN = "fintrack-deps-v1"


def normalized_requirements(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            # -r / -c / --index-url would make the id depend on other files or places.
            raise ValueError(f"unsupported requirements option: {line.split()[0]}")
        lines.append("".join(line.split()).lower())
    return sorted(set(lines))


def deps_id(text: str, target: str = TARGET) -> str:
    payload = "\n".join([DOMAIN, target, *normalized_requirements(text)]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    print(deps_id(Path(argv[0]).read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
