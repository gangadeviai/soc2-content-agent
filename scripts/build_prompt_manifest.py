"""Regenerate prompts/manifest.json from the files on disk.

Run from a PR, never on a server. CI fails if the committed manifest differs
from the output of this script (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "prompts"


def build() -> dict:
    existing = (
        json.loads((ROOT / "manifest.json").read_text()) if (ROOT / "manifest.json").exists() else {"versions": {}}
    )
    versions = {}
    for vdir in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        files = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(vdir.glob("*.md"))}
        prev = existing["versions"].get(vdir.name, {})
        versions[vdir.name] = {"status": prev.get("status", "pending_review"), "files": files}
    return {"schema": 1, "versions": versions}


if __name__ == "__main__":
    manifest = build()
    out = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        current = (ROOT / "manifest.json").read_text()
        if current != out:
            print("manifest.json is stale - run scripts/build_prompt_manifest.py", file=sys.stderr)
            sys.exit(1)
        print("manifest up to date")
    else:
        (ROOT / "manifest.json").write_text(out)
        print(out)
