# Kinetic SDK Migration Roadmap

## Purpose

Track incremental migration toward a stable src-layout package structure with compatibility-first guardrails.

## Milestones

1. Baseline and Rollback
- Create baseline tag from common base commit.
- Confirm rollback and recovery workflow.

2. Validation Gates on UV
- Standardize test execution on `uv run python -m pytest`.
- Ensure package build succeeds with `uv build`.

3. Compatibility-First Package Moves
- Promote new tools into `kinetic_devops/` first.
- Add `src/kinetic_devops/` shims for migration continuity.

4. CLI and Test Matrix Alignment
- Keep CLI matrix and module coverage synchronized as modules move.
- Preserve `python -m kinetic_devops ...` behavior during migration.

5. Progressive Src Cutover
- Migrate module ownership in small batches.
- Validate each batch with UV test + build gates.

6. Final Cutover and Retirement
- Remove transitional shims once src-path ownership is complete.
- Re-run full UV validation and publish release candidate.

## Current Status (2026-08-27)

- Baseline tag created and pushed: `baseline-pre-migration-2026-08-27`
- UV pytest parity configured and passing.
- UV build passing.
- Export all-companies with dedup implemented.
- `trim_trace_paths` promoted and shimmed for src migration.
