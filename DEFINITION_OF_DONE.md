# Definition of Done

This document defines release gates for kinetic-devops.

A change is "Done" only when all Required Now gates pass and any deferred items are documented in the PR/release notes.

## Release Gates (Required Now)

### 1. Product and Documentation Readiness
- Feature behavior matches documented usage in README and command help.
- New or changed CLI behavior includes at least one usage example.
- Changelog entry exists for externally visible behavior changes.
- No broken imports, command aliases, or documented command paths.

### 2. Regression Testing
- Existing tests pass for impacted areas.
- At least one regression test is added for each bug fix that changed behavior.
- Entrypoint and routing changes must include regression coverage for:
	- top-level router behavior
	- independently runnable submodules (`uv run python -m kinetic_devops.<module>`)
	- warning regressions (for example, RuntimeWarning reintroduction)
- New tests must run in CI-compatible non-interactive mode.

### 3. Packaging and Installability
- Package builds successfully as both sdist and wheel.
- Distribution metadata validates via twine check.
- Console entry point is verified (`uv run kinetic-devops`).
- Module entry point is verified (`uv run python -m kinetic_devops`).

### 4. CI/CD Baseline
- GitHub Actions build pipeline runs successfully for release tags.
- PyPI trusted publishing configuration is valid and operational.
- Release tag pattern is respected (`v*`).
- Protected branch policy is respected (merge via PR unless emergency override).

### 5. Security and Operational Hygiene
- No secrets or credential artifacts added to tracked files.
- .gitignore and scanning posture are unchanged or improved.
- Any known security or redaction gaps are documented in changelog/release notes.

## Minimum Evidence Required In PR

Every PR that changes runtime behavior should include:
- Test evidence (command + pass result)
- Build evidence (`uv build`)
- Metadata evidence (`uvx twine check dist/*`)
- CLI smoke evidence for changed commands

## Deferred Gates (Adopt Next)

These are high-value and low-friction additions:

1. CI matrix smoke tests
- Run core test suite on Python 3.10, 3.12, and latest.

2. PR quality gates
- Require one approving review on release-impacting changes.
- Require passing checks before merge.

3. Automated version guard
- Fail release workflow if project version and tag do not match.

4. Artifact retention and provenance
- Retain build artifacts for troubleshooting.
- Keep attestations enabled in publish action.

5. Test selection policy
- Maintain a small required regression subset for fast PR checks.
- Keep full suite on release tags.

## Validation Commands

```bash
# full local validation
uv run python -m tests.test_runner --validate

# targeted and full tests
uv run python -m unittest tests.test_entry_router tests.test_cli
uv run python -m tests.test_runner

# packaging checks
uv build
uvx twine check dist/*

# entrypoint smoke
uv run python -m kinetic_devops --help
uv run kinetic-devops --help

# optional (machine-global PATH check, non-blocking)
kinetic-devops --help
```

## Definition of Done Decision

Release is approved when:
- All Required Now gates pass.
- Deferred Minimal-Effort Upgrades (if any) are explicitly tracked.
- Release notes document known limitations and risk.

---

Last Updated: April 2, 2026
Current Stage: Alpha hardening with regression-first release discipline
