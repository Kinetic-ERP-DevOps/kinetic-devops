# Git-Based, uv-Managed Migration Plan

## Goal

Adopt a modern `src/` package layout while maintaining repository stability through controlled Git workflows, compatibility-first migration, and uv-managed validation.

The migration is managed through branches, tags, validation, and staged cutovers, not through large-scale filesystem reorganization.

---

## Guiding Principles

- Preserve current behavior until the new structure is proven.
- Use Git branches, tags, and PRs as the primary migration controls.
- Keep the repository operational throughout the transition.
- Maintain compatibility during migration through shims and transitional support where needed.
- Use `uv` as the canonical dependency, build, and test workflow.
- Make documentation the source of truth for architecture, workflows, and migration status.
- Remove legacy paths only after successful validation and cutover.
- Prefer incremental, reversible changes over large rewrites.

---

## Phase 0 — Baseline and Inventory

### Objectives

1. Create a rollback point.

```bash
git tag baseline-pre-migration
```

or

```bash
git checkout -b migration/baseline
```

2. Capture the current repository state:

- package layout
- `pyproject.toml`
- uv configuration
- CLI and entrypoints
- scripts and tooling
- tests
- documentation

3. Document any migration work already in progress.

4. Create:

```text
Documents/migration_tracker.md
```

with:

```text
ComponentCurrentTargetStatusOwnerPR
```

### Validation

```bash
uv sync --frozen
uv run pytest
uv build
```

### Outputs

- Baseline tag or branch
- Migration inventory
- Test/build baseline
- Migration tracker

---

## Phase 1 — Stabilize the Baseline

### Objectives

- Verify builds and tests.
- Document known failures.
- Freeze major structural changes until the baseline is understood.
- Reconcile documentation with actual behavior.

### Validation

```bash
uv sync --frozen
uv run pytest
uv build
```

### Outputs

- Verified baseline
- Known issue list
- Validated documentation

---

## Phase 2 — Introduce the Target Structure

### Target Layout

```text
src/
└── kinetic_devops/

scripts/

tests/

Documents/
```

### Process

Create a migration branch:

```bash
git checkout -b migration/src-layout
```

Introduce the new structure incrementally while preserving:

- package imports
- CLI behavior
- entrypoints
- packaging workflows

Compatibility mechanisms may include:

- package discovery updates
- import shims
- temporary dual-path support
- shared entrypoints

Legacy structure remains supported until validation is complete.

### Validation

After every structural change:

```bash
uv sync --frozen
uv run pytest
uv build
```

### Outputs

- Target structure established
- Existing imports continue working
- Packaging remains valid

---

## Phase 3 — Documentation Standardization

### Canonical Documentation Structure

```text
README.md

Documents/
├── ARCHITECTURE.md
├── migration_plan.md
├── migration_tracker.md
└── ROADMAP.md

scripts/
└── README.md

tests/
└── README.md
```

### Standard Terminology

- Package: Installable Python package
- Module: Python source module
- Script: Standalone utility
- Example: Demonstration code
- Legacy Layout: Current repository structure
- Src Layout: Target repository structure

### Validation

All documented workflows must execute successfully:

```bash
uv sync --frozen
uv run pytest
uv build
```

### Outputs

- Consistent documentation
- Standardized terminology
- No conflicting instructions

---

## Phase 4 — Incremental Migration

### Branch Strategy

Short-lived migration branches:

- `migration/runtime-package`
- `migration/entrypoints`
- `migration/scripts`
- `migration/tests`
- `migration/docs`

Long-lived branches:

- `main`
- `release/*`

Migration branches should be merged and deleted after successful validation.

### Migration Workflow

For each migration PR:

1. Implement one logical change.
2. Preserve compatibility.
3. Update tests.
4. Update documentation.
5. Validate.
6. Review and merge.

### Validation

```bash
uv sync --frozen
uv run pytest
uv build
```

### Recommended Order

1. Runtime package structure
2. Entry points and CLI
3. Scripts and tooling
4. Tests and examples
5. Documentation and references

### Outputs

- Small, reviewable changes
- Continuous validation
- Minimal migration risk

---

## Phase 4.5 — Release Candidate Validation

### Objectives

Validate the new layout before final cutover.

Create release candidate tags:

```bash
git tag RC_v1.0.0
```

or use:

```text
release/*
```

branches.

### Validation

```bash
uv sync --frozen
uv run pytest
uv build
```

Validate installation and entrypoints from a clean environment.

### Outputs

- Validated release candidate
- Confirmed packaging and runtime behavior

---

## Phase 5 — Cutover and Retirement

### Preconditions

The following must succeed from the new structure:

```bash
uv sync --frozen
uv run pytest
uv build
```

### Process

Create a cutover branch:

```bash
git checkout -b migration/cutover
```

Then:

- remove compatibility-only layers
- retire deprecated paths
- update documentation
- close migration tracker items

Finalize with:

```bash
git tag migration-complete-v1
```

### Outputs

- Canonical structure active
- Legacy paths retired
- Migration complete

---

## Rollback Criteria

A migration change should be reverted rather than patched forward if it causes:

- packaging failures
- broken entrypoints
- consumer import breakage
- test regressions
- undocumented behavior changes
- failed build validation

---

## Guardrails

Every migration PR must:

- have a narrow scope
- be independently reviewable
- include test updates
- include documentation updates
- pass validation

Required validation:

```bash
uv sync --frozen
uv run pytest
uv build
```

Avoid:

- mass deletions
- large structural rewrites
- rename-only migrations without compatibility
- removing legacy paths before cutover validation

---

## Recommended Delivery Sequence

### PR 1 — Baseline and Inventory

```bash
git tag baseline-pre-migration
```

- inventory current structure
- add migration tracker
- document uv workflows

### PR 2 — Compatibility-First src Layout

```bash
git checkout -b migration/src-layout
```

- introduce `src/kinetic_devops`
- preserve compatibility
- validate continuously

### PR 3 — Documentation Standardization

- architecture documentation
- migration status documentation
- workflow documentation
- terminology cleanup

### PR 4 — Release Candidate Validation

```bash
git tag RC_v1.0.0
```

- validate packaging
- validate installation
- validate entrypoints

### PR 5 — Cutover and Cleanup

```bash
git tag migration-complete-v1
```

- remove compatibility layers
- retire legacy paths
- finalize documentation
- complete migration tracker

---

## Definition of Done

- `src/` layout is the canonical repository structure.
- All dependency management, testing, and builds run through `uv`.
- `uv sync --frozen` succeeds.
- `uv run pytest` succeeds.
- `uv build` succeeds.
- Built artifacts install successfully in a clean environment.
- Entry points behave consistently with the pre-migration baseline unless explicitly documented.
- All structural changes are traceable through branches, tags, and PRs.
- Documentation accurately reflects the repository.
- Migration tracker contains no open migration items.
- Legacy compatibility layers are removed or have documented retirement plans.
- Final migration tag exists.

Migration philosophy: Baseline → Compatibility → Validation → Release Candidate → Cutover → Retirement.
This keeps the repository stable, reversible, and continuously releasable throughout the migration.
