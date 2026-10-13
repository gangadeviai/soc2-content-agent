# Prompt changelog

Every entry corresponds to a Pull Request that touched `prompts/`. The PR must:
1. Bump the version directory (`prompts/<semver>/`) - a version holds **all** stage prompts.
2. Regenerate `manifest.json` with `python scripts/build_prompt_manifest.py`.
3. Pass `tests/test_prompt_registry.py` and `tests/test_injection.py` (prompt-injection regression set).
4. Be approved by a CODEOWNER from `@marketing-platform-security`.
5. Record the approval date and the production rollout date below.

| Version | Status | PR | Approved by | Rolled out | Summary |
|---|---|---|---|---|---|
| 1.2.0 | approved | #3 | (fill in) | (fill in) | `system.md` gains SEO / AEO / GEO rules matching the new `seo`, `aeo`, `geo` schema fields and the deterministic checker in `app/seo.py`. Other files unchanged from 1.1.0. |
| 1.1.0 | deprecated | #2 | (fill in) | (fill in) | Adds `brand_analysis.md` and `strategy.md` stage prompts. |
| 1.0.0 | deprecated | #1 | (fill in) | (fill in) | Initial writing prompt and injection guard. Lacks stage prompts; runtime refuses to load it. |
