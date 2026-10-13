# Migration Validation Snapshot (2026-08-27)

## Scope

Validation for migration branch `feature/export-all-companies-dedup` after introducing:
- all-companies export orchestration with de-duplication
- package promotion for `trim_trace_paths`
- UV pytest parity configuration
- migration tracker and roadmap documentation

## Rollback Marker

- Baseline tag: `baseline-pre-migration-2026-08-27`
- Baseline commit: `4c225762802ca7a52e39cebafd666766b2cad030`
- Tag pushed to `origin`.

## Validation Commands

```bash
uv run python -m pytest -q
uv build
```

## Results

- `uv run python -m pytest -q`: PASS
  - 114 passed, 2 warnings, 93 subtests passed
- `uv build`: PASS
  - sdist and wheel generated successfully

## Notes

- On this Windows environment, `uv run pytest` executable resolution can fail due to process spawn permissions.
- `uv run python -m pytest` is the reliable equivalent and is now documented as canonical in repo docs.

## Remote Policy

- Migration and feature branch pushes are restricted to `origin` unless explicitly approved.
