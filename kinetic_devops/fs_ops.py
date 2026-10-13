"""Shared filesystem helpers for safe write behavior across modules."""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple


def _run_git(args: list[str], cwd: str) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()
    except Exception:
        return 1, ""


def find_repo_root(path: str) -> Optional[str]:
    abs_path = os.path.abspath(path)
    cwd = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path) or "."
    code, out = _run_git(["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or not out:
        return None
    return os.path.abspath(out)


def _parse_check_ignore_verbose_line(line: str) -> Dict[str, str]:
    # Expected format: <source>:<line>:<pattern>\t<path>
    # Example: .gitignore:12:exports/\texports/file.zip
    entry = {
        "source": "",
        "line": "",
        "pattern": "",
        "path": "",
    }
    if not line:
        return entry

    left, sep, right = line.partition("\t")
    if sep:
        entry["path"] = right.strip()

    parts = left.split(":", 2)
    if len(parts) == 3:
        entry["source"] = parts[0].strip()
        entry["line"] = parts[1].strip()
        entry["pattern"] = parts[2].strip()
    return entry


def _ignore_pattern_type(pattern: str) -> str:
    p = str(pattern or "").strip()
    if p.startswith("!"):
        p = p[1:]
    # Path-scoped ignore rules include directory separators.
    return "path" if "/" in p or "\\" in p else "ext"


def _collect_ignore_matches(repo_root: str, rel_path: str) -> List[Dict[str, str]]:
    matches: List[Dict[str, str]] = []
    candidates = [rel_path]

    parent = os.path.dirname(rel_path).replace("\\", "/")
    if parent:
        parts = [p for p in parent.split("/") if p]
        acc: List[str] = []
        for part in parts:
            acc.append(part)
            candidates.append("/".join(acc))

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        code, out = _run_git(["check-ignore", "-v", "--", candidate], repo_root)
        if code != 0 or not out:
            continue
        for line in out.splitlines():
            parsed = _parse_check_ignore_verbose_line(line.strip())
            if parsed.get("pattern"):
                matches.append(parsed)

    return matches


def _rank_risk(level: str) -> int:
    order = {
        "none": 0,
        "low": 1,
        "medium": 2,
        "high": 3,
        "critical": 4,
    }
    return order.get(str(level).lower(), 0)


def required_force_flag(risk_level: str) -> str:
    level = str(risk_level or "").lower()
    mapping = {
        "medium": "--force-medium",
        "high": "--force-high",
        "critical": "--force-critical",
    }
    return mapping.get(level, "")


def is_write_permitted(
    risk_level: str,
    force: bool = False,
    force_low: bool = False,
    force_medium: bool = False,
    force_high: bool = False,
    force_critical: bool = False,
    no_force_low: bool = False,
    no_force_none: bool = False,
) -> Tuple[bool, str]:
    """Evaluate overwrite permission using the granular force matrix."""
    level = str(risk_level or "none").lower()

    if force:
        return True, "global_force"

    if level == "none":
        if no_force_none:
            return False, "no_force_none"
        return True, "none_default"

    if level == "low":
        if no_force_low and not force_low:
            return False, "no_force_low"
        return True, "low_default"

    if level == "medium":
        if force_medium:
            return True, "force_medium"
        return False, "require_force_medium"

    if level == "high":
        if force_high:
            return True, "force_high"
        return False, "require_force_high"

    if level == "critical":
        if force_critical:
            return True, "force_critical"
        return False, "require_force_critical"

    # Unknown levels default to blocked.
    return False, "unknown_risk"


def describe_overwrite_risk(path: str) -> Dict[str, object]:
    abs_path = os.path.abspath(path)
    exists = os.path.exists(abs_path)
    repo_root = find_repo_root(abs_path)
    info: Dict[str, object] = {
        "exists": exists,
        "in_repo": False,
        "tracked": False,
        "untracked": False,
        "ignored": False,
        "repo_root": repo_root or "",
        "ignore_type": "",
        "ignore_pattern": "",
        "ignore_source": "",
        "ignore_line": "",
        "risk_level": "none",
        "system_action": "proceed",
        "reason": "new_path",
    }

    if not exists:
        return info

    if not repo_root:
        # Outside git: existing file with no repository safety net.
        info["untracked"] = bool(exists)
        info["risk_level"] = "critical"
        info["system_action"] = "block"
        info["reason"] = "outside_repo"
        return info

    rel_path = os.path.relpath(abs_path, repo_root).replace("\\", "/")
    info["in_repo"] = not rel_path.startswith("..")
    if not info["in_repo"]:
        info["untracked"] = bool(exists)
        info["risk_level"] = "critical"
        info["system_action"] = "block"
        info["reason"] = "outside_repo"
        return info

    code_tracked, _ = _run_git(["ls-files", "--error-unmatch", "--", rel_path], repo_root)
    tracked = code_tracked == 0
    info["tracked"] = tracked
    if tracked:
        info["risk_level"] = "low"
        info["system_action"] = "proceed"
        info["reason"] = "tracked"
        return info

    code_ignored, _ = _run_git(["check-ignore", "--quiet", "--", rel_path], repo_root)
    ignored = code_ignored == 0
    info["ignored"] = ignored

    if ignored:
        matches = _collect_ignore_matches(repo_root, rel_path)
        ignore_types = [_ignore_pattern_type(m.get("pattern", "")) for m in matches]
        highest_ignore_type = "path" if "path" in ignore_types else "ext"

        # Use the first path-based match when available, else first match.
        selected = None
        if matches:
            for m in matches:
                if _ignore_pattern_type(m.get("pattern", "")) == "path":
                    selected = m
                    break
            if not selected:
                selected = matches[0]

        if selected:
            info["ignore_pattern"] = selected.get("pattern", "")
            info["ignore_source"] = selected.get("source", "")
            info["ignore_line"] = selected.get("line", "")

        info["ignore_type"] = highest_ignore_type
        if highest_ignore_type == "path":
            info["risk_level"] = "high"
            info["system_action"] = "block"
            info["reason"] = "ignored_path"
        else:
            info["risk_level"] = "medium"
            info["system_action"] = "block"
            info["reason"] = "ignored_ext"
        return info

    info["untracked"] = bool(exists and not ignored)
    if info["untracked"]:
        info["risk_level"] = "critical"
        info["system_action"] = "block"
        info["reason"] = "untracked"
    else:
        info["risk_level"] = "none"
        info["system_action"] = "proceed"
        info["reason"] = "new_path"

    return info


def safe_atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
