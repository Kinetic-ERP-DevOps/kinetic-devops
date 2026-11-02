# Trace Toggle — Design

**Date:** 2026-10-13
**Status:** Draft — awaiting review
**Motivating case:** intercompany PO (ICPO) make-to-order jobs created without a Group Code (`JobHead.ProdCode`)

## Problem

Diagnosing Kinetic transaction issues (e.g. ICPO → sales order → job → firm/release) needs
full-fidelity traces. Today, capturing them is manual: someone raises logging in the UI, reproduces
the issue, finds and downloads the logs, then remembers to turn logging back down. Source traces
get lost, raw (unredacted) files get shared over Teams, and elevated logging can be left on.

## Goal

One API-driven tool that:

1. Snapshots the **current** logging configuration.
2. Raises logging to **maximum**.
3. Holds an **interaction window** while a user (e.g. the person who reported it) reproduces the issue in Kinetic.
4. **Pulls specific logs** for that window, reduced and redacted.
5. **Reverts** to the snapshot — reliably, even if the window is interrupted.

## Non-goals

- Driving the Kinetic UI (no browser automation). The user performs the steps by hand.
- Replacing the Kinetic web client's debug tool export.
- Analysing the logs (that stays a manual / separate step).

## Kinetic surfaces (from `exports/Kinetic-API-Store/methods`)

| Scope | Service | Read | Write | Notes |
|---|---|---|---|---|
| `user` | `Ice.BO.UserTracingSvc` | `GetByID(userID)`, `GetRows` | `GetNewOrGetByID`, `GetNewUserTracingFlag`, `Update` | `UserTracing` row: `DataTrace`, `DataTraceFullDataSets`, `DataTraceReturnData`, `DataTraceDSChanges`, `DataTraceCCDataSet`, `DataTraceIncludeServerTrace`, … `UserTracingFlag` rows: `ServerTraceFlag` |
| `server` | `Ice.BO.AdminCompanySvc` | `GetServerLogTraceSettings` | `SetServerLogTraceSettings(traceDS)` | `traceDS` shape undocumented (spec says `object`). May be blocked on SaaS. |
| `process` | `Erp.Proc.MultiCompanySvc`, `Erp.Proc.MultiCompanyDirectSvc` (later: `AutoJobFirmSvc`, `MRPSvc`) | process parameters | process parameters | `LogLevel` parameter. |

| Log source | Service | Notes |
|---|---|---|
| `server-log` | `Ice.Lib.FileTransferSvc` `DownloadFile` / `DownloadFileForCompany` / `DownloadFileForUser` (`folder:int`, `serverPath:str`) | Folder enum meaning and SaaS availability unknown. |
| `task-log` | `Ice.BO.SysTaskLogSvc` | Process/task logs (Multi-Company, AutoJobFirm). |
| `api-trace` | `Ice.BO.APITraceSvc.GetList` | Per service-method call counts and timings per user; no payloads. |

### Open questions (resolved by `discover`, plan task 1)

1. Shape of `traceDS` returned by `GetServerLogTraceSettings`.
2. Valid `ServerTraceFlag` values (from `GetNewUserTracingFlag` / existing rows).
3. `FileTransferSvc` `folder` enum values; whether server logs are downloadable on SaaS; whether
   `returnObj` is base64.
4. Whether the caller may `SetServerLogTraceSettings` on SaaS.
5. How a process's `LogLevel` is persisted (process defaults vs. per-run parameter) and therefore
   whether it can be snapshotted/restored at all.

Any scope or source that `discover` shows to be unavailable is disabled (reported as such), not
worked around.

## Design

### Module and CLI

- New module `kinetic_devops/trace_toggle.py`; client class `KineticTraceService(KineticBaseClient)`
  reusing `execute_request` (wire logging + redaction already built in).
- Registered as `trace` in `TOOLS` in `kinetic_devops/__main__.py`.
- Common args: `--env`, `--user` (admin running the tool), consistent with other subcommands.

```
trace discover  [--target-user U]                                   # read-only probe
trace capture   --target-user U [--scope user|server|process ...] [--max-minutes N]
                [--source NAME ...] [--watchdog] [--no-reduce]
trace up        --target-user U [--scope ...] [--max-minutes N] [--watchdog]
trace pull      --session ID [--source NAME ...] [--no-reduce]
trace down      --session ID
trace status
```

Defaults: `--scope user`, `--max-minutes 30`, `--source api-trace` plus whichever of
`task-log` / `server-log` `discover` proves usable.

### Scopes

Each scope is a small object with four operations:

- `read() -> dict` — current state (used for snapshot and verification).
- `apply_max(state) -> None` — write the max profile.
- `restore(snapshot_state) -> None` — write the snapshot back.
- `diff(expected, actual) -> list[str]` — field-level drift report.

Max profiles are a data table in the module (not scattered literals), e.g. for `user`: every
`DataTrace*` boolean `true`, plus `ServerTraceFlag` rows from the list `discover` confirms.

### Snapshot file

`temp/trace_sessions/<env>/<session-id>.json` (under git-ignored `temp/`):

```json
{
  "session_id": "...", "env": "...", "admin_user": "...", "target_user": "...",
  "scopes": ["user"], "sources": ["api-trace"],
  "window_start": "ISO-8601", "window_end": null, "deadline": "ISO-8601",
  "state": "applied | pulled | restored | restore_failed",
  "watchdog_task": null,
  "before": { "user": { ... } }
}
```

### `capture` flow

1. **Preflight.** Check for overdue/unrestored snapshots for this env + target user; refuse if one
   exists (prevents snapshotting a max state as "current"). Print them with the `down` command.
2. **up.** For each scope: `read()` → write snapshot → `apply_max()`. If any scope fails, restore
   the scopes already changed and exit non-zero. Optionally register the watchdog. Print window
   start, deadline, and the exact recovery command.
3. **Window.** Wait until the first of: Enter, `--max-minutes` deadline, Ctrl+C. Record
   `window_end`.
4. **pull** (inside `try`). Fetch each source filtered to `[window_start, window_end]` and the
   target user. Raw output → `temp/trace_sessions/<id>/raw/` (decoded if base64; never printed).
   Unless `--no-reduce`: run `trim_trace_paths --call-context-policy purge-empty` into
   `reduced/`, then `find_sensitive_data --fail-on-findings` on `reduced/`. Write `manifest.json`
   only when the scan is clean.
5. **down** (in `finally`, always runs). `restore()` each scope, re-`read()`, `diff()` against
   snapshot. Clean → `state=restored`, remove watchdog task. Drift/failure → `state=restore_failed`,
   print the drift and the retry command, exit non-zero.

Revert has priority over pull: a pull failure never skips `down`; raw files are kept so `pull
--session` can be re-run afterwards (it uses the window stored in the snapshot).

### Standalone commands

- `up` / `pull` / `down` run the same steps independently, keyed by `--session`, so a capture can
  span terminals or a meeting.
- `down` is idempotent and works from a fresh terminal (all state is in the snapshot).
- `status` lists snapshots not in `restored`, flagging any past `deadline`.
- Every `trace` invocation first checks for overdue snapshots and offers to revert them.

### Watchdog (opt-in, `--watchdog`)

`up` registers a one-shot Windows Task Scheduler job at `deadline + 5 min` running
`kinetic-devops trace down --session <id>`. A successful `down` deletes it. Its name is stored in
the snapshot. If registration fails, `up` warns and continues (the other safeguards still apply).
Caveat documented: the watchdog needs a valid stored session at revert time.

### Error handling and safety

- Raw logs only ever under `temp/trace_sessions/<id>/raw/`; nothing raw printed or copied elsewhere.
- No write call is made before its snapshot has been persisted to disk.
- Per-scope success/failure reported; partial `up` is rolled back.
- Non-zero exit on: refused preflight, partial `up`, sensitive-data findings, restore drift.

## Testing

- `tests/test_trace_toggle.py`, mocking `execute_request` with fixtures recorded by `discover`
  (redacted before commit):
  - snapshot → max → restore round trip leaves state equal to snapshot;
  - `up` refuses when an unrestored snapshot exists;
  - partial `up` failure rolls back already-changed scopes;
  - `down` reports drift and sets `restore_failed`;
  - `capture` runs `down` after a pull exception and after KeyboardInterrupt;
  - `pull` writes no manifest when the sensitive scan reports findings;
  - `down` is idempotent.
- Add `trace` to the CLI matrix (`tests/cli_matrix.py`) if `test_cli_matrix_consistency.py`
  requires it.
- Manual acceptance (non-production environments only): one `capture` against a test user, confirm settings return to
  their exact prior values via `discover`.

## Out of scope / later

- Additional process scopes (`AutoJobFirmSvc`, `MRPSvc`) beyond Multi-Company.
- Cross-platform watchdog (cron/launchd).
- Automated analysis of pulled logs.
