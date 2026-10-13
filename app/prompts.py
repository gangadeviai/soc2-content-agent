"""Prompt registry - prompts are code (CC8.1 change management).

Prompts live under ``prompts/<version>/`` in Git. ``prompts/manifest.json`` pins
the SHA-256 of every file. At runtime the registry refuses to load a prompt whose
hash does not match the manifest, so a prompt cannot be edited on a server or in
a UI without that edit showing up as an integrity failure.

One version directory holds every stage prompt (brand analysis, strategy,
writing) plus the shared injection guard, so a single version string in the
audit record identifies the exact behaviour of the whole pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

REQUIRED_FILES = ("system.md", "injection_guard.md", "brand_analysis.md", "strategy.md")


class PromptIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptBundle:
    version: str
    files: dict[str, tuple[str, str]]  # name -> (text, sha256)

    def text(self, name: str) -> str:
        return self.files[name][0]

    def sha256(self, name: str) -> str:
        return self.files[name][1]

    @property
    def system(self) -> str:
        return self.text("system.md")

    @property
    def system_sha256(self) -> str:
        return self.sha256("system.md")

    @property
    def injection_guard(self) -> str:
        return self.text("injection_guard.md")

    @property
    def lineage(self) -> dict[str, str]:
        out = {"prompt_version": self.version}
        for name, (_, sha) in sorted(self.files.items()):
            out[f"{name.removesuffix('.md')}_sha256"] = sha
        return out


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PromptRegistry:
    def __init__(self, prompts_dir: Path):
        self.prompts_dir = prompts_dir
        self.manifest = json.loads((prompts_dir / "manifest.json").read_text(encoding="utf-8"))

    def load(self, version: str) -> PromptBundle:
        if version not in self.manifest["versions"]:
            raise PromptIntegrityError(f"prompt version {version!r} not in manifest")
        entry = self.manifest["versions"][version]
        if entry.get("status") not in {"approved", "deprecated"}:
            raise PromptIntegrityError(f"prompt version {version!r} is not approved for production")
        vdir = self.prompts_dir / version
        files: dict[str, tuple[str, str]] = {}
        for name, expected in entry["files"].items():
            path = vdir / name
            actual = sha256_file(path)
            if actual != expected:
                raise PromptIntegrityError(
                    f"{path} hash {actual[:12]} != manifest {expected[:12]} - prompt was modified outside Git"
                )
            files[name] = (path.read_text(encoding="utf-8"), actual)
        missing = [n for n in REQUIRED_FILES if n not in files]
        if missing:
            raise PromptIntegrityError(f"prompt version {version!r} is missing stage prompts: {missing}")
        return PromptBundle(version=version, files=files)
