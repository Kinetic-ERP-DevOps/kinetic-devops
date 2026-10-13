# Migration Tracker

| Component | Current | Target | Status | Owner | PR |
|---|---|---|---|---|---|
| Runtime package layout | kinetic_devops/ | src/kinetic_devops/ | In Progress (compat shim active) | DevOps | feature/export-all-companies-dedup |
| Export orchestration | single-company run | native all-companies + dedup | Implemented (feature branch) | DevOps | feature/export-all-companies-dedup |
| Trace trimming utility | temp/trim_trace_paths.py | package module + src shim | Implemented (compat shim active) | DevOps | feature/export-all-companies-dedup |
| CLI matrix coverage | existing modules | include migrated modules | Implemented | DevOps | feature/export-all-companies-dedup |
| Pytest uv parity | direct unittest via hooks | uv run python -m pytest scoped to tests | Implemented | DevOps | feature/export-all-companies-dedup |
| Build validation | ad-hoc | uv build gate | Implemented | DevOps | feature/export-all-companies-dedup |
| Baseline rollback marker | none | baseline migration tag on origin | Implemented (`baseline-pre-migration-2026-08-27`) | DevOps | feature/export-all-companies-dedup |
