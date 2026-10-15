import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request


RISK_PATH_KEYWORDS = [
    "auth",
    "security",
    "token",
    "credential",
    "secret",
    "sensitive",
    "router",
    "find_sensitive_data",
]

CONVENTIONAL_PREFIXES = (
    "feat",
    "fix",
    "refactor",
    "chore",
    "docs",
    "test",
    "perf",
    "build",
    "ci",
    "revert",
)

HIGH_RISK_FILE_KEYWORDS = [
    "auth",
    "security",
    "token",
    "credential",
    "find_sensitive_data",
    "router",
]

DEAD_END_NAME_HINTS = [
    "backup",
    "old",
    "archive",
    "temp",
]


@dataclass
class CmdResult:
    code: int
    out: str
    err: str


def _env(name: str, default: str) -> str:
    value = str(os.getenv(name, "")).strip()
    return value if value else default


def _default_active_root() -> str:
    return _env("KINETIC_ACTIVE_ROOT", r"D:\Kinetic_SDK")


def _default_temp_path(file_name: str) -> str:
    return str(Path(_default_active_root()) / "temp" / file_name)


def _default_ignore_patterns() -> list[str]:
    raw = str(os.getenv("KINETIC_LEGACY_IGNORE", "")).strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return ["Epicor-Rest-PostmanSamples", "OneDrive - Epicor Users Group"]


# Named profile chosen with --ai-profile for this run (None = the stored default).
_ACTIVE_AI_PROFILE: str | None = None


def set_active_ai_profile(name: str | None) -> None:
    """Select which stored AI profile this process uses (None = the default)."""
    global _ACTIVE_AI_PROFILE
    previous = _ACTIVE_AI_PROFILE
    _ACTIVE_AI_PROFILE = (name or "").strip() or None
    if _ACTIVE_AI_PROFILE and not _stored_ai_profile():
        missing, _ACTIVE_AI_PROFILE = _ACTIVE_AI_PROFILE, previous
        raise ValueError(
            f"No AI profile named '{missing}'. "
            "List them with 'uv run python -m kinetic_devops.auth ai-show'."
        )


def _resolve_ai_base_url(raw_value: str | None) -> str:
    """--ai-base-url, else the selected (or default) profile stored with
    'auth ai-set' (keyring), else the KINETIC_AI_BASE_URL fallback for
    users/runners without a stored profile."""
    value = str(raw_value or "").strip()
    if value:
        return value

    stored = str(_stored_ai_profile().get("base_url") or "").strip()
    if stored:
        return stored

    env_value = str(os.getenv("KINETIC_AI_BASE_URL", "")).strip()
    if env_value:
        return env_value

    raise ValueError(
        "AI base URL is required. Store one with 'uv run python -m kinetic_devops.auth ai-set', "
        "pass --ai-base-url, or set KINETIC_AI_BASE_URL."
    )


def _stored_ai_profile() -> dict[str, Any]:
    """The selected (or default) AI profile stored by 'auth ai-set', or {}."""
    try:
        from kinetic_devops.auth import get_ai_profile

        return get_ai_profile(_ACTIVE_AI_PROFILE)
    except Exception:
        return {}


def _profile_for_url(base_url: str) -> dict[str, Any]:
    """The selected profile when it is for base_url; otherwise any stored
    profile for that URL (so an explicit --ai-base-url still gets its style)."""
    url = normalize_base_url(base_url)
    selected = _stored_ai_profile()
    if normalize_base_url(str(selected.get("base_url") or "")) == url:
        return selected
    try:
        from kinetic_devops.auth import list_ai_profiles

        for profile in list_ai_profiles()[1].values():
            if normalize_base_url(str(profile.get("base_url") or "")) == url:
                return profile
    except Exception:
        pass
    return {}


def _ai_client_settings(base_url: str) -> tuple[str, dict[str, str]]:
    """(api_style, extra headers) for base_url: the style of the matching stored
    profile (default openai); the API key is looked up by URL."""
    url = normalize_base_url(base_url)
    style = _profile_for_url(url).get("api_style")
    headers: dict[str, str] = {}
    try:
        from kinetic_devops.auth import get_ai_api_key

        key = get_ai_api_key(url)
    except Exception:
        key = ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return (style or "openai"), headers


def select_model(models: list[str], base_url: str) -> str:
    """The matching profile's default model when the server offers it, else the first listed."""
    preferred = str(_profile_for_url(base_url).get("model") or "").strip()
    if preferred and preferred in models:
        return preferred
    return models[0]


def run_git(repo: Path, args: list[str], timeout: int = 60) -> CmdResult:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
        return CmdResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        return CmdResult(124, out, err or f"timeout after {timeout}s")


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def fetch_models(base_url: str, timeout: int) -> list[str]:
    style, headers = _ai_client_settings(base_url)
    path = "/api/models" if style == "openwebui" else "/v1/models"
    req = request.Request(url=f"{normalize_base_url(base_url)}{path}", headers=headers, method="GET")
    with request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    data = payload.get("data") or []
    return [item.get("id", "") for item in data if isinstance(item, dict) and item.get("id")]


def call_ai(base_url: str, timeout: int, model: str, prompt: str, max_tokens: int = 360) -> dict[str, Any]:
    style, headers = _ai_client_settings(base_url)
    if style == "openwebui":
        # Open WebUI exposes the chat API only; the prompt becomes a single user message.
        url = f"{normalize_base_url(base_url)}/api/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.15,
            "max_tokens": max_tokens,
            "stream": False,
        }
    else:
        url = f"{normalize_base_url(base_url)}/v1/completions"
        body = {
            "model": model,
            "prompt": prompt,
            "temperature": 0.15,
            "max_tokens": max_tokens,
            "stream": False,
        }
    req = request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def extract_text(ai_response: dict[str, Any]) -> str:
    choices = ai_response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str):
                return text.strip()
            message = first.get("message")  # chat-completions response shape
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"].strip()
    return ""


def truncate_text(text: str, max_chars: int = 4500) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def parse_changed_paths_from_patch(patch_text: str) -> list[str]:
    paths: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ b/"):
            p = line.replace("+++ b/", "", 1).strip()
            if p and p not in paths:
                paths.append(p)
    return paths


def risk_from_paths(paths: list[str]) -> str:
    joined = " ".join(paths).lower()
    if any(key in joined for key in RISK_PATH_KEYWORDS):
        return "high"
    if paths:
        return "medium"
    return "low"


def is_rogue_commit(subject: str, paths: list[str]) -> bool:
    normalized = subject.strip().lower()
    conventional = bool(re.match(r"^([a-z]+)(\(|:)", normalized)) and normalized.split("(", 1)[0].split(":", 1)[0] in CONVENTIONAL_PREFIXES
    if not conventional:
        return True
    return risk_from_paths(paths) == "high"


def build_snapshot_item(repo: Path) -> dict[str, Any]:
    status = run_git(repo, ["status", "--porcelain=v1"], timeout=30)
    unstaged = run_git(repo, ["diff", "--", "."], timeout=60)
    staged = run_git(repo, ["diff", "--cached", "--", "."], timeout=60)

    snapshot_patch = (staged.out or "") + "\n" + (unstaged.out or "")
    paths = parse_changed_paths_from_patch(snapshot_patch)

    return {
        "id": "snapshot-working-tree",
        "type": "snapshot",
        "status": status.out.strip(),
        "paths": paths,
        "risk_hint": risk_from_paths(paths),
        "payload": truncate_text(snapshot_patch or "No diff"),
    }


def build_commit_items(repo: Path, base_ref: str, max_commits: int) -> list[dict[str, Any]]:
    rev_range = f"{base_ref}..HEAD"
    revs = run_git(repo, ["rev-list", "--reverse", f"--max-count={max_commits}", rev_range], timeout=30)
    if revs.code != 0:
        return []

    commits = [line.strip() for line in revs.out.splitlines() if line.strip()]
    items: list[dict[str, Any]] = []
    for sha in commits:
        meta = run_git(
            repo,
            ["show", "--no-patch", "--format=%H%n%an%n%ae%n%ad%n%s", sha],
            timeout=30,
        )
        patch = run_git(
            repo,
            ["show", sha, "--pretty=format:", "--patch", "--stat"],
            timeout=120,
        )

        meta_lines = [x for x in meta.out.splitlines()]
        subject = meta_lines[4] if len(meta_lines) >= 5 else ""
        paths = parse_changed_paths_from_patch(patch.out)
        rogue = is_rogue_commit(subject, paths)

        items.append(
            {
                "id": sha,
                "type": "commit",
                "sha": sha,
                "author": meta_lines[1] if len(meta_lines) > 1 else "",
                "email": meta_lines[2] if len(meta_lines) > 2 else "",
                "date": meta_lines[3] if len(meta_lines) > 3 else "",
                "subject": subject,
                "paths": paths,
                "risk_hint": risk_from_paths(paths),
                "rogue_candidate": rogue,
                "payload": truncate_text(patch.out or ""),
            }
        )
    return items


def build_prompt(repo: str, base_ref: str, item: dict[str, Any]) -> str:
    common = (
        "You are reviewing one local git delta for regression and functionality-loss risk.\n"
        "Policy context: D:/Kinetic_SDK is active authoritative root.\n"
        "Do not request raw secrets or unrelated code. Use the provided git metadata and diff only.\n"
        "Return concise markdown with these exact sections:\n"
        "1) Change Classification\n"
        "2) Functionality Loss Risk\n"
        "3) Regression Risk\n"
        "4) Dead-End or Rogue Signals\n"
        "5) Next Verification Checks (no commit actions)\n"
        "6) GO/NO-GO for further promotion\n"
    )

    header = {
        "repo": repo,
        "base_ref": base_ref,
        "item_id": item.get("id"),
        "item_type": item.get("type"),
        "risk_hint": item.get("risk_hint"),
        "rogue_candidate": item.get("rogue_candidate", False),
        "subject": item.get("subject", ""),
        "paths": item.get("paths", []),
        "status": item.get("status", ""),
    }

    return common + "\nDelta metadata:\n" + json.dumps(header, indent=2) + "\n\nDiff payload:\n" + item.get("payload", "")


def build_retry_prompt(repo: str, base_ref: str, item: dict[str, Any]) -> str:
    compact = {
        "repo": repo,
        "base_ref": base_ref,
        "item_id": item.get("id"),
        "item_type": item.get("type"),
        "risk_hint": item.get("risk_hint"),
        "rogue_candidate": item.get("rogue_candidate", False),
        "subject": item.get("subject", ""),
        "paths": item.get("paths", []),
        "status": item.get("status", ""),
    }
    return (
        "Review this single git delta for regression/functionality-loss risk. "
        "Return markdown with short bullets for: classification, risks, checks, go/no-go.\n\n"
        + json.dumps(compact, indent=2)
    )


def render_master_summary(run_report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Local AI Incremental Diff Review")
    lines.append("")
    lines.append(f"- Repository: {run_report['repo']}")
    lines.append(f"- Base Ref: {run_report['base_ref']}")
    lines.append(f"- Model: {run_report['model']}")
    lines.append(f"- Items Reviewed: {len(run_report['items'])}")
    lines.append("")
    lines.append("## Items")
    for item in run_report["items"]:
        label = item.get("type")
        ident = item.get("id")
        rogue = item.get("rogue_candidate", False)
        out_md = item.get("output_md")
        lines.append(f"- {label}: {ident} | rogue_candidate={rogue} | report={out_md}")
    lines.append("")
    lines.append("## Note")
    lines.append("- These are analysis-only outputs. No commit, push, or branch rewrite actions are performed.")
    return "\n".join(lines)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _load_report_files(reports_dir: Path, pattern: str) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for path in sorted(reports_dir.glob(pattern)):
        payload = _load_json(path)
        if isinstance(payload, dict):
            reports.append({"path": str(path), "payload": payload})
    return reports


def _find_active_repo_entry(compiled: dict[str, Any], active_repo: str) -> dict[str, Any] | None:
    reports = compiled.get("reports")
    if not isinstance(reports, list):
        return None

    for report in reports:
        samples = report.get("sample_candidates")
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if str(sample.get("candidate", "")).lower() == active_repo.lower():
                return sample
    return None


def _build_candidate_matrix(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_candidate: dict[str, dict[str, Any]] = {}

    for report in reports:
        report_path = report["path"]
        payload = report["payload"]
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            continue

        for row in candidates:
            if not isinstance(row, dict):
                continue

            raw_candidate = str(row.get("candidate", "")).strip()
            if not raw_candidate:
                continue

            candidate_key = raw_candidate.replace("\\", "/")
            entry = by_candidate.setdefault(
                candidate_key,
                {
                    "candidate": raw_candidate,
                    "resolved_paths": [],
                    "seen_in_reports": [],
                    "exists_values": [],
                    "git_values": [],
                    "branches": [],
                    "status_samples": [],
                    "stashes": [],
                },
            )

            entry["seen_in_reports"].append(report_path)
            resolved = row.get("resolved_path")
            if isinstance(resolved, str) and resolved and resolved not in entry["resolved_paths"]:
                entry["resolved_paths"].append(resolved)

            entry["exists_values"].append(bool(row.get("exists")))
            entry["git_values"].append(bool(row.get("git_repo")))

            branch = row.get("branch")
            if isinstance(branch, str) and branch and branch not in entry["branches"]:
                entry["branches"].append(branch)

            status = row.get("status")
            if isinstance(status, list):
                for line in status[:8]:
                    if isinstance(line, str) and line not in entry["status_samples"]:
                        entry["status_samples"].append(line)

            stashes = row.get("stashes")
            if isinstance(stashes, list):
                for line in stashes[:8]:
                    if isinstance(line, str) and line not in entry["stashes"]:
                        entry["stashes"].append(line)

    matrix: list[dict[str, Any]] = []
    for value in by_candidate.values():
        candidate_text = value["candidate"]
        normalized = candidate_text.lower().replace("\\", "/")

        duplicate_mount = "/mnt/c/users/" in normalized
        dead_end_name = any(name in normalized for name in DEAD_END_NAME_HINTS)

        exists_any = any(value["exists_values"])
        exists_all = all(value["exists_values"]) if value["exists_values"] else False
        git_any = any(value["git_values"])

        lost_progress_risk = "low"
        if exists_any and not git_any:
            lost_progress_risk = "medium"
        if exists_any and not git_any and dead_end_name:
            lost_progress_risk = "high"

        dead_end_risk = "low"
        if duplicate_mount:
            dead_end_risk = "high"
        elif dead_end_name and not git_any:
            dead_end_risk = "medium"

        contamination_risk = "low"
        if len(value["stashes"]) > 0 and git_any:
            contamination_risk = "medium"
        if duplicate_mount:
            contamination_risk = "high"

        matrix.append(
            {
                "candidate": candidate_text,
                "seen_count": len(value["seen_in_reports"]),
                "exists_any": exists_any,
                "exists_all": exists_all,
                "git_repo_any": git_any,
                "branches": value["branches"],
                "status_samples": value["status_samples"],
                "stashes": value["stashes"],
                "duplicate_mount": duplicate_mount,
                "dead_end_name": dead_end_name,
                "lost_progress_risk": lost_progress_risk,
                "contamination_risk": contamination_risk,
                "dead_end_risk": dead_end_risk,
                "resolved_paths": value["resolved_paths"],
                "seen_in_reports": value["seen_in_reports"],
            }
        )

    matrix.sort(key=lambda x: x["candidate"].lower())
    return matrix


def _matches_any(candidate: str, patterns: list[str]) -> bool:
    c = candidate.lower().replace("\\", "/")
    for p in patterns:
        if p.lower().replace("\\", "/") in c:
            return True
    return False


def _filter_candidates(
    matrix: list[dict[str, Any]],
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> list[dict[str, Any]]:
    filtered = matrix
    if include_patterns:
        filtered = [row for row in filtered if _matches_any(row["candidate"], include_patterns)]
    if exclude_patterns:
        filtered = [row for row in filtered if not _matches_any(row["candidate"], exclude_patterns)]
    return filtered


def _regression_risk_for_active_entry(active_entry: dict[str, Any] | None) -> dict[str, Any]:
    if not active_entry:
        return {
            "active_entry_found": False,
            "risk": "unknown",
            "reasons": ["No active-root candidate found in compiled report"],
        }

    status = active_entry.get("status") or []
    changed_files = [line for line in status if isinstance(line, str) and line.startswith(" M ")]
    changed_text = " ".join(changed_files).lower()
    high_keyword_hit = any(key in changed_text for key in HIGH_RISK_FILE_KEYWORDS)

    risk = "low"
    reasons = [f"Changed tracked files in active root: {len(changed_files)}"]
    if len(changed_files) > 0:
        risk = "medium"
    if high_keyword_hit:
        risk = "high"
        reasons.append("High-risk keyword present in changed file path(s)")

    return {
        "active_entry_found": True,
        "risk": risk,
        "reasons": reasons,
        "branch": active_entry.get("branch"),
        "status": status,
        "stashes": active_entry.get("stashes") or [],
    }


def _render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Non-Commit Functionality Retention Review")
    lines.append("")
    lines.append(f"- Active Root: {summary['active_root']}")
    lines.append(f"- Reports Scanned: {summary['report_count']}")
    lines.append(f"- Candidates Scanned: {summary['candidate_count']}")
    lines.append("")

    lines.append("## 1) Functionality Loss Signals")
    high_lost = [c for c in summary["candidates"] if c["lost_progress_risk"] == "high"]
    med_lost = [c for c in summary["candidates"] if c["lost_progress_risk"] == "medium"]
    if not high_lost and not med_lost:
        lines.append("- No medium/high lost-progress signals detected from metadata")
    else:
        for row in high_lost + med_lost:
            lines.append(
                f"- {row['candidate']} -> lost-progress risk {row['lost_progress_risk']} (exists_any={row['exists_any']}, git_repo_any={row['git_repo_any']})"
            )

    lines.append("")
    lines.append("## 2) Regression Risk in Active Root")
    reg = summary["active_root_regression"]
    lines.append(f"- Overall risk: {reg['risk']}")
    for reason in reg.get("reasons", []):
        lines.append(f"- {reason}")
    if reg.get("branch"):
        lines.append(f"- Branch: {reg['branch']}")
    status = reg.get("status") or []
    for line in status[:8]:
        lines.append(f"- status: {line}")

    lines.append("")
    lines.append("## 3) Dead-End Capture Signals")
    dead_high = [c for c in summary["candidates"] if c["dead_end_risk"] == "high"]
    dead_med = [c for c in summary["candidates"] if c["dead_end_risk"] == "medium"]
    if not dead_high and not dead_med:
        lines.append("- No significant dead-end capture signals detected")
    else:
        for row in dead_high + dead_med:
            lines.append(
                f"- {row['candidate']} -> dead-end risk {row['dead_end_risk']} (duplicate_mount={row['duplicate_mount']}, dead_end_name={row['dead_end_name']})"
            )

    lines.append("")
    lines.append("## 4) No-Commit Safe Passes")
    lines.append("- Pass A (read-only): regenerate focused metadata from target candidates only")
    lines.append("- Pass B (read-only): compare candidate presence across reports and drop duplicate /mnt/c/Users mirror entries")
    lines.append("- Pass C (read-only): track active-root modified files and run targeted checks before any commit")
    lines.append("- Pass D (read-only): maintain a dead-end allowlist for backup/old/archive paths")

    lines.append("")
    lines.append("## 5) Proprietary vs Kinetic_Devops Intent (Metadata Heuristic)")
    for row in summary["candidates"]:
        name = row["candidate"].lower().replace("\\", "/")
        label = "unknown"
        if "kinetic-dev" in name and not name.endswith("kinetic_sdk"):
            label = "likely proprietary or legacy working copy"
        if "kinetic_audit_env" in name or "u_kinetic-dev" in name:
            label = "likely experiment or migration environment"
        if "backup" in name or "old" in name:
            label = "likely archive/dead-end"
        lines.append(f"- {row['candidate']} -> {label}")

    return "\n".join(lines)


def _run_diff_review(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_ref = args.base_ref.strip()
    if not base_ref:
        upstream = run_git(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        base_ref = upstream.out.strip() if upstream.code == 0 and upstream.out.strip() else "HEAD~1"

    ai_base_url = _resolve_ai_base_url(args.ai_base_url)

    models = fetch_models(ai_base_url, args.ai_timeout)
    if not models:
        raise RuntimeError("No local AI models available")
    model = select_model(models, ai_base_url)

    items = [build_snapshot_item(repo)]
    items.extend(build_commit_items(repo, base_ref=base_ref, max_commits=args.max_commits))

    reviewed_items: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        prompt = build_prompt(str(repo), base_ref, item)
        try:
            ai_resp = call_ai(ai_base_url, args.ai_timeout, model, prompt, max_tokens=360)
            ai_text = extract_text(ai_resp)
            error = ""
        except Exception as exc:
            retry_prompt = build_retry_prompt(str(repo), base_ref, item)
            try:
                ai_resp = call_ai(ai_base_url, max(20, args.ai_timeout // 2), model, retry_prompt, max_tokens=220)
                ai_text = extract_text(ai_resp)
                error = f"retry_used_after: {exc}"
                prompt = retry_prompt
            except Exception as retry_exc:
                ai_resp = {"error": str(retry_exc)}
                ai_text = "(AI call failed for this item)"
                error = f"initial: {exc}; retry: {retry_exc}"

        stem = f"{idx:03d}_{item['type']}_{item['id'].replace('/', '_').replace(':', '_')}"
        json_path = out_dir / f"{stem}.json"
        md_path = out_dir / f"{stem}.md"

        json_payload = {
            "item": item,
            "prompt": prompt,
            "response": ai_resp,
            "error": error,
        }
        json_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")
        md_path.write_text(ai_text, encoding="utf-8")

        enriched = dict(item)
        enriched["output_json"] = str(json_path)
        enriched["output_md"] = str(md_path)
        enriched["ai_error"] = error
        reviewed_items.append(enriched)

    run_report = {
        "repo": str(repo),
        "base_ref": base_ref,
        "model": model,
        "items": reviewed_items,
    }

    master_json = out_dir / "run_report.json"
    master_md = out_dir / "run_summary.md"
    master_json.write_text(json.dumps(run_report, indent=2), encoding="utf-8")
    master_md.write_text(render_master_summary(run_report), encoding="utf-8")

    print(f"BASE_REF={base_ref}")
    print(f"MODEL={model}")
    print(f"ITEM_COUNT={len(reviewed_items)}")
    print(f"RUN_REPORT_JSON={master_json}")
    print(f"RUN_SUMMARY_MD={master_md}")
    return 0


def _run_no_commit_review(args: argparse.Namespace) -> int:
    reports = _load_report_files(Path(args.reports_dir), args.reports_glob)
    candidate_matrix = _build_candidate_matrix(reports)
    candidate_matrix = _filter_candidates(candidate_matrix, args.include_pattern, args.exclude_pattern)

    compiled = _load_json(Path(args.compiled_file)) or {}
    active_entry = _find_active_repo_entry(compiled, args.active_root)
    active_regression = _regression_risk_for_active_entry(active_entry)

    summary = {
        "active_root": args.active_root,
        "report_count": len(reports),
        "candidate_count": len(candidate_matrix),
        "active_root_regression": active_regression,
        "candidates": candidate_matrix,
    }

    json_out = Path(args.json_out)
    md_out = Path(args.md_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)

    json_out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md_out.write_text(_render_markdown(summary), encoding="utf-8")

    print(f"JSON_REPORT={json_out}")
    print(f"MD_REPORT={md_out}")
    print(f"REPORT_COUNT={len(reports)}")
    print(f"CANDIDATE_COUNT={len(candidate_matrix)}")
    print(f"ACTIVE_ROOT_REGRESSION_RISK={active_regression['risk']}")
    return 0


def _should_ignore(repo_path: str, ignore_patterns: list[str]) -> bool:
    normalized = repo_path.lower().replace("\\", "/")
    for pattern in ignore_patterns:
        if pattern.lower().replace("\\", "/") in normalized:
            return True
    return False


def _default_repo_list_from_inventory(inventory_file: Path) -> list[str]:
    if not inventory_file.exists():
        return []

    payload = json.loads(inventory_file.read_text(encoding="utf-8", errors="replace"))
    repos: list[Any]
    if isinstance(payload, dict):
        repos = payload.get("repos") if isinstance(payload.get("repos"), list) else []
    elif isinstance(payload, list):
        repos = payload
    else:
        repos = []

    results: list[str] = []
    for entry in repos:
        if not isinstance(entry, dict):
            continue
        root = entry.get("repo") or entry.get("repo_root") or entry.get("candidate") or ""
        root = str(root).strip()
        if root:
            results.append(root)
    return results


def _dedupe_repos(repos: list[str]) -> list[str]:
    unique_repos: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        key = str(repo).strip().lower().replace("\\", "/")
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        unique_repos.append(str(repo).strip())
    return unique_repos


def _run_legacy_intake(args: argparse.Namespace) -> int:
    explicit_repos = [r for r in args.repo if str(r).strip()]
    repos = explicit_repos or _default_repo_list_from_inventory(Path(args.inventory_file))
    unique_repos = _dedupe_repos(repos)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    active_key = str(args.active_root).lower().replace("\\", "/")
    ai_base_url = _resolve_ai_base_url(args.ai_base_url)

    summary: dict[str, Any] = {
        "active_root": args.active_root,
        "inventory_file": args.inventory_file,
        "ai_base_url": ai_base_url,
        "ai_timeout": args.ai_timeout,
        "max_commits": args.max_commits,
        "base_ref": args.base_ref,
        "repos": [],
    }

    for repo in unique_repos:
        repo_path = Path(repo)
        normalized = str(repo_path)
        normalized_key = normalized.lower().replace("\\", "/")

        if _should_ignore(normalized, args.ignore):
            summary["repos"].append(
                {
                    "repo": normalized,
                    "status": "ignored",
                    "reason": "matches ignore pattern",
                }
            )
            continue

        if normalized_key == active_key:
            summary["repos"].append(
                {
                    "repo": normalized,
                    "status": "skipped",
                    "reason": "active authoritative root",
                }
            )
            continue

        if not repo_path.exists():
            summary["repos"].append(
                {
                    "repo": normalized,
                    "status": "missing",
                    "reason": "path does not exist",
                }
            )
            continue

        if not (repo_path / ".git").exists():
            summary["repos"].append(
                {
                    "repo": normalized,
                    "status": "not-git",
                    "reason": "no .git directory",
                }
            )
            continue

        safe_name = normalized.replace(":", "").replace("\\", "_").replace("/", "_").replace(" ", "_")
        repo_out = out_dir / safe_name

        diff_args = argparse.Namespace(
            repo=normalized,
            base_ref=args.base_ref,
            max_commits=args.max_commits,
            ai_base_url=ai_base_url,
            ai_timeout=args.ai_timeout,
            out_dir=str(repo_out),
        )

        try:
            code = _run_diff_review(diff_args)
            status = "ok" if code == 0 else "error"
            error = ""
        except Exception as exc:
            code = 1
            status = "error"
            error = str(exc)

        summary["repos"].append(
            {
                "repo": normalized,
                "status": status,
                "exit_code": code,
                "out_dir": str(repo_out),
                "error": error,
            }
        )

    status_counts: dict[str, int] = {}
    for row in summary["repos"]:
        key = str(row.get("status", "unknown"))
        status_counts[key] = status_counts.get(key, 0) + 1
    summary["status_counts"] = status_counts

    summary_file = out_dir / "legacy_intake_run_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"SUMMARY_FILE={summary_file}")
    print(f"REPO_COUNT={len(summary['repos'])}")
    print(f"STATUS_COUNTS={json.dumps(status_counts, sort_keys=True)}")
    return 0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _verify_commit_item(repo: Path, item: dict[str, Any]) -> dict[str, Any]:
    sha = str(item.get("sha") or item.get("id") or "").strip()
    result: dict[str, Any] = {
        "id": item.get("id"),
        "type": item.get("type"),
        "sha": sha,
        "ok": True,
        "errors": [],
        "warnings": [],
    }

    if not sha:
        result["ok"] = False
        result["errors"].append("missing commit sha")
        return result

    exists = run_git(repo, ["cat-file", "-e", f"{sha}^{{commit}}"], timeout=20)
    if exists.code != 0:
        result["ok"] = False
        result["errors"].append("commit no longer reachable in local object database")
        return result

    subject_cmd = run_git(repo, ["show", "--no-patch", "--format=%s", sha], timeout=30)
    paths_cmd = run_git(repo, ["show", "--name-only", "--pretty=format:", sha], timeout=60)

    subject_actual = subject_cmd.out.strip()
    subject_recorded = str(item.get("subject", "")).strip()
    if subject_actual != subject_recorded:
        result["ok"] = False
        result["errors"].append(
            f"subject mismatch recorded='{subject_recorded}' actual='{subject_actual}'"
        )

    actual_paths: list[str] = []
    for line in paths_cmd.out.splitlines():
        p = line.strip()
        if p and p not in actual_paths:
            actual_paths.append(p)

    recorded_paths = [str(p) for p in item.get("paths", []) if str(p).strip()]
    if sorted(actual_paths) != sorted(recorded_paths):
        result["ok"] = False
        result["errors"].append("path set mismatch between recorded report and git")

    expected_risk = risk_from_paths(recorded_paths)
    if str(item.get("risk_hint", "")).strip() != expected_risk:
        result["ok"] = False
        result["errors"].append("risk_hint mismatch with deterministic path heuristic")

    expected_rogue = is_rogue_commit(subject_recorded, recorded_paths)
    if _truthy(item.get("rogue_candidate")) != expected_rogue:
        result["ok"] = False
        result["errors"].append("rogue_candidate mismatch with deterministic rule")

    return result


def _verify_snapshot_item(item: dict[str, Any]) -> dict[str, Any]:
    paths = [str(p) for p in item.get("paths", []) if str(p).strip()]
    expected_risk = risk_from_paths(paths)
    recorded_risk = str(item.get("risk_hint", "")).strip()

    result: dict[str, Any] = {
        "id": item.get("id"),
        "type": item.get("type"),
        "ok": True,
        "errors": [],
        "warnings": [],
    }

    if recorded_risk != expected_risk:
        result["ok"] = False
        result["errors"].append("snapshot risk_hint mismatch with deterministic path heuristic")

    payload = str(item.get("payload", "")).strip()
    if not payload:
        result["ok"] = False
        result["errors"].append("snapshot payload missing")

    return result


def _render_verify_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Verify Report")
    lines.append("")
    lines.append(f"- Source Run Report: {report['source_run_report']}")
    lines.append(f"- Repository: {report['repo']}")
    lines.append(f"- Checked Items: {report['checked_items']}")
    lines.append(f"- Passed Items: {report['passed_items']}")
    lines.append(f"- Failed Items: {report['failed_items']}")
    lines.append(f"- Artifact Warnings: {report['artifact_warnings']}")
    lines.append(f"- Strict Mode: {report['strict']}")
    lines.append("")

    lines.append("## Deterministic Findings")
    failures = [r for r in report["item_results"] if not _truthy(r.get("ok"))]
    if not failures:
        lines.append("- All deterministic checks passed")
    else:
        for row in failures:
            lines.append(f"- {row.get('type')} {row.get('id')}: FAIL")
            for err in row.get("errors", []):
                lines.append(f"  - {err}")

    if report["artifact_missing"]:
        lines.append("")
        lines.append("## Artifact Warnings")
        for path in report["artifact_missing"]:
            lines.append(f"- Missing artifact path: {path}")

    lines.append("")
    lines.append("## Recommendation")
    if report["failed_items"] == 0:
        lines.append("- VERIFIED: deterministic checks match stored analysis metadata")
    else:
        lines.append("- REVIEW REQUIRED: deterministic mismatches found; do not treat AI output as trusted until reconciled")
    return "\n".join(lines)


def _run_verify_report(args: argparse.Namespace) -> int:
    run_report_path = Path(args.run_report)
    run_report = _load_json(run_report_path)
    if not isinstance(run_report, dict):
        raise ValueError(f"Could not parse run report JSON: {run_report_path}")

    repo = Path(args.repo)
    items = run_report.get("items")
    if not isinstance(items, list):
        raise ValueError("Invalid run report format: items[] missing")

    item_results: list[dict[str, Any]] = []
    artifact_missing: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type", "")).strip()
        if item_type == "commit":
            result = _verify_commit_item(repo, item)
        elif item_type == "snapshot":
            result = _verify_snapshot_item(item)
        else:
            result = {
                "id": item.get("id"),
                "type": item_type or "unknown",
                "ok": False,
                "errors": ["unsupported item type"],
                "warnings": [],
            }

        for artifact_key in ("output_json", "output_md"):
            artifact_path = str(item.get(artifact_key, "")).strip()
            if artifact_path and not Path(artifact_path).exists():
                artifact_missing.append(artifact_path)

        item_results.append(result)

    failed_items = sum(1 for r in item_results if not _truthy(r.get("ok")))
    passed_items = len(item_results) - failed_items

    verify_report = {
        "source_run_report": str(run_report_path),
        "repo": str(repo),
        "checked_items": len(item_results),
        "passed_items": passed_items,
        "failed_items": failed_items,
        "artifact_warnings": len(artifact_missing),
        "artifact_missing": artifact_missing,
        "strict": bool(args.strict),
        "item_results": item_results,
    }

    out_json = Path(args.json_out)
    out_md = Path(args.md_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(verify_report, indent=2), encoding="utf-8")
    out_md.write_text(_render_verify_markdown(verify_report), encoding="utf-8")

    print(f"VERIFY_JSON={out_json}")
    print(f"VERIFY_MD={out_md}")
    print(f"CHECKED_ITEMS={len(item_results)}")
    print(f"FAILED_ITEMS={failed_items}")
    print(f"ARTIFACT_WARNINGS={len(artifact_missing)}")

    if args.strict and failed_items > 0:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    active_root_default = _default_active_root()
    parser = argparse.ArgumentParser(
        description=(
            "Analysis workflows (local AI + deterministic metadata checks) over git repositories.\n"
            "Paths default to <active root>/temp/..., where the active root is KINETIC_ACTIVE_ROOT (or a\n"
            "built-in default). Most defaults can also be set with KINETIC_* environment variables.\n"
            "Output files are overwritten in place."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.analysis diff-review --repo . --ai-base-url http://localhost:1234\n"
            "  python -m kinetic_devops.analysis verify-report --repo . --strict"
        ),
    )
    subparsers = parser.add_subparsers(dest="analysis_command", required=True, metavar="<command>")

    ai_profile_help = "Stored AI profile to use (see 'auth ai-show'; default: the default profile)"
    ai_url_help = (
        "AI server base URL (default: the profile stored with 'auth ai-set', else KINETIC_AI_BASE_URL; "
        "one of them is required)"
    )

    diff = subparsers.add_parser(
        "diff-review",
        help="Incremental snapshot plus commit local AI review",
        description=(
            "Reviews the repo's uncommitted working-tree diff plus each commit in <base-ref>..HEAD with an "
            "AI model (the selected profile's default model when the server offers it, else the first "
            "model the server lists). The diffs and patches are SENT TO THE AI SERVER. "
            "Writes one JSON + Markdown file per item, plus run_report.json and run_summary.md, into "
            "--out-dir."
        ),
    )
    diff.add_argument("--repo", metavar="PATH", default=active_root_default, help="Git repository to review (default: active root)")
    diff.add_argument(
        "--base-ref", metavar="REF", default="",
        help="Review commits after this ref (default: the branch's upstream, else HEAD~1)",
    )
    diff.add_argument(
        "--max-commits", metavar="N", type=int, default=int(_env("KINETIC_ANALYSIS_MAX_COMMITS", "20")),
        help="Maximum number of commits to review (default: KINETIC_ANALYSIS_MAX_COMMITS or 20)",
    )
    diff.add_argument("--ai-base-url", metavar="URL", default="", help=ai_url_help)
    diff.add_argument("--ai-profile", metavar="NAME", default="", help=ai_profile_help)
    diff.add_argument(
        "--ai-timeout", metavar="SECONDS", type=int, default=int(_env("KINETIC_AI_TIMEOUT", "180")),
        help="Per-request AI timeout in seconds (default: KINETIC_AI_TIMEOUT or 180)",
    )
    diff.add_argument(
        "--out-dir", metavar="DIR", default=_env("KINETIC_ANALYSIS_DIFF_OUT_DIR", _default_temp_path("snapshot_commit_ai")),
        help="Output directory (default: KINETIC_ANALYSIS_DIFF_OUT_DIR or <active root>/temp/snapshot_commit_ai)",
    )

    ncr = subparsers.add_parser(
        "no-commit-review",
        help="Non-commit functionality/regression risk summary",
        description=(
            "Offline: reads existing triage report JSON files and a compiled master report, builds a matrix "
            "of candidate functionality and a regression-risk rating for the active root, and writes it as "
            "JSON and Markdown. No AI or network calls."
        ),
    )
    ncr.add_argument(
        "--reports-dir", metavar="DIR", default=_env("KINETIC_REPORTS_DIR", _default_temp_path("")),
        help="Directory holding triage reports (default: KINETIC_REPORTS_DIR or <active root>/temp)",
    )
    ncr.add_argument(
        "--reports-glob", metavar="GLOB", default=_env("KINETIC_REPORTS_GLOB", "triage_report_autoai*.json"),
        help="File pattern for triage reports in --reports-dir (default: KINETIC_REPORTS_GLOB or "
             "triage_report_autoai*.json)",
    )
    ncr.add_argument(
        "--compiled-file", metavar="PATH",
        default=_env("KINETIC_COMPILED_REPORT", _default_temp_path("triage_compiled_master.json")),
        help="Compiled master report JSON (default: KINETIC_COMPILED_REPORT or <active root>/temp/"
             "triage_compiled_master.json)",
    )
    ncr.add_argument("--active-root", metavar="PATH", default=active_root_default, help="Repository treated as the active root")
    ncr.add_argument(
        "--include-pattern", metavar="PATTERN", action="append", default=[],
        help="Only keep candidates matching this pattern (repeatable)",
    )
    ncr.add_argument(
        "--exclude-pattern", metavar="PATTERN", action="append", default=[],
        help="Drop candidates matching this pattern (repeatable)",
    )
    ncr.add_argument(
        "--json-out", metavar="PATH",
        default=_env("KINETIC_ANALYSIS_NO_COMMIT_JSON", _default_temp_path("non_commit_functionality_review.json")),
        help="JSON summary output (default: <active root>/temp/non_commit_functionality_review.json)",
    )
    ncr.add_argument(
        "--md-out", metavar="PATH",
        default=_env("KINETIC_ANALYSIS_NO_COMMIT_MD", _default_temp_path("non_commit_functionality_review.md")),
        help="Markdown summary output (default: <active root>/temp/non_commit_functionality_review.md)",
    )

    legacy = subparsers.add_parser(
        "legacy-intake",
        help="Run incremental diff review across legacy repositories",
        description=(
            "Runs diff-review on each repository given with --repo (or listed in --inventory-file), skipping "
            "ignored paths, the active root, missing paths and non-git folders. Diffs are SENT TO THE AI "
            "SERVER. Per-repo results go under --out-dir plus legacy_intake_run_summary.json."
        ),
    )
    legacy.add_argument(
        "--inventory-file", metavar="PATH",
        default=_env("KINETIC_REMOTE_REPO_INVENTORY", _default_temp_path("remote_repo_inventory.json")),
        help="Repo inventory JSON used when no --repo is given (default: KINETIC_REMOTE_REPO_INVENTORY or "
             "<active root>/temp/remote_repo_inventory.json)",
    )
    legacy.add_argument("--repo", metavar="PATH", action="append", default=[], help="Repository to review (repeatable)")
    legacy.add_argument(
        "--active-root", metavar="PATH", default=active_root_default,
        help="Repository to skip as the active root",
    )
    legacy.add_argument(
        "--ignore", metavar="SUBSTRING", action="append", default=_default_ignore_patterns(),
        help="Skip repos whose path contains this text (repeatable; adds to KINETIC_LEGACY_IGNORE or built-in defaults)",
    )
    legacy.add_argument("--base-ref", metavar="REF", default="", help="Base ref per repo (default: upstream, else HEAD~1)")
    legacy.add_argument(
        "--max-commits", metavar="N", type=int, default=int(_env("KINETIC_ANALYSIS_MAX_COMMITS", "10")),
        help="Maximum commits per repo (default: KINETIC_ANALYSIS_MAX_COMMITS or 10)",
    )
    legacy.add_argument("--ai-base-url", metavar="URL", default="", help=ai_url_help)
    legacy.add_argument("--ai-profile", metavar="NAME", default="", help=ai_profile_help)
    legacy.add_argument(
        "--ai-timeout", metavar="SECONDS", type=int, default=int(_env("KINETIC_AI_TIMEOUT", "45")),
        help="Per-request AI timeout in seconds (default: KINETIC_AI_TIMEOUT or 45)",
    )
    legacy.add_argument(
        "--out-dir", metavar="DIR",
        default=_env("KINETIC_ANALYSIS_LEGACY_OUT_DIR", _default_temp_path("legacy_intake_reviews")),
        help="Output directory (default: KINETIC_ANALYSIS_LEGACY_OUT_DIR or <active root>/temp/legacy_intake_reviews)",
    )

    verify = subparsers.add_parser(
        "verify-report",
        help="Deterministically verify a prior analysis run report",
        description=(
            "Offline: re-checks a diff-review run_report.json against git (commit still reachable, subject "
            "and changed paths match, risk/rogue flags match the deterministic rules) and that its output "
            "files exist. Writes JSON and Markdown verify reports. No AI calls."
        ),
    )
    verify.add_argument("--repo", metavar="PATH", default=active_root_default, help="Git repository the run report refers to")
    verify.add_argument(
        "--run-report",
        metavar="PATH",
        default=_env("KINETIC_ANALYSIS_VERIFY_SOURCE", _default_temp_path("snapshot_commit_ai/run_report.json")),
        help="run_report.json to verify (default: <active root>/temp/snapshot_commit_ai/run_report.json)",
    )
    verify.add_argument(
        "--json-out",
        metavar="PATH",
        default=_env("KINETIC_ANALYSIS_VERIFY_JSON", _default_temp_path("snapshot_commit_ai/verify_report.json")),
        help="JSON verify report output (default: next to the default run report)",
    )
    verify.add_argument(
        "--md-out",
        metavar="PATH",
        default=_env("KINETIC_ANALYSIS_VERIFY_MD", _default_temp_path("snapshot_commit_ai/verify_report.md")),
        help="Markdown verify report output (default: next to the default run report)",
    )
    verify.add_argument("--strict", action="store_true", help="Exit with code 1 when deterministic checks fail")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        set_active_ai_profile(getattr(args, "ai_profile", "") or None)
        if args.analysis_command == "diff-review":
            raise SystemExit(_run_diff_review(args))
        if args.analysis_command == "no-commit-review":
            raise SystemExit(_run_no_commit_review(args))
        if args.analysis_command == "legacy-intake":
            raise SystemExit(_run_legacy_intake(args))
        if args.analysis_command == "verify-report":
            raise SystemExit(_run_verify_report(args))
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(2)

    raise SystemExit(2)


if __name__ == "__main__":
    main()