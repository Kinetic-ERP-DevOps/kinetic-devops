#!/usr/bin/env python3
"""Trim large trace JSON files by removing irrelevant JSON paths.

Examples:
  python trim_trace_paths.py UseExistingSN.json --preset kinetic-heavy --in-place
  python trim_trace_paths.py "*.json" --drop "*.request.headers.Authorization" --out-dir reduced
  python trim_trace_paths.py dump.json --rules-file rules.txt --pretty
    python trim_trace_paths.py dump.json --delta-mode summary --delta-format jsonc
"""

from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import datetime
import fnmatch
import glob
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

INDEX_RE = re.compile(r"\[\d+\]")
PLACEHOLDER_REPEAT_RE = re.compile(r"(\{[A-Za-z0-9_]+\})(?:\1)+")
ESCAPED_PLACEHOLDER_RE = re.compile(r"\\\{([A-Za-z0-9_]+)\\\}")
LEGACY_BRACE_CLASS_ARTIFACT_RE = re.compile(r"\\\}\[A-Za-z\]\+\\\}")
HOST_AND_INSTANCE_RE = re.compile(r"[A-Za-z0-9]+\.epicorsaas\.com/[A-Za-z0-9]+")
HOST_ONLY_RE = re.compile(r"[A-Za-z0-9]+\.epicorsaas\.com")
INSTANCE_TOKEN_RE = re.compile(r"\bSaaS[A-Za-z0-9]+\b")
REDACTIONS_ENV_VAR = "KINETIC_TRACE_REDACTIONS"
DEFAULT_REDACTIONS_PATH = Path.home() / ".kinetic-devops" / "trace_redactions.json"


def load_redaction_terms(path: str | None = None) -> dict[str, list[str]]:
    """Load site-specific redaction terms (company IDs, plant patterns, names,
    employee IDs, usernames) from a local JSON file -- never from the code, so
    no organization's data ships with the tool. Path: `path`, else
    $KINETIC_TRACE_REDACTIONS, else ~/.kinetic-devops/trace_redactions.json.
    A missing file means no site-specific rules; the generic ones (emails,
    hosts, URL parameters, long tokens) still apply."""
    source = Path(path or os.environ.get(REDACTIONS_ENV_VAR) or DEFAULT_REDACTIONS_PATH)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    keys = ("company_ids", "plant_patterns", "names", "employee_ids", "usernames")
    return {key: [str(v) for v in data.get(key) or [] if str(v).strip()] for key in keys}


def _word_regex(alternatives: list[str], flags: int = 0) -> re.Pattern | None:
    if not alternatives:
        return None
    return re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(alternatives) + r")(?![A-Za-z0-9_])", flags)


def _literal_alternatives(terms: list[str]) -> list[str]:
    # Longest first, so "First Last" wins over "First"; whitespace matches any run of spaces.
    return [re.escape(t).replace(r"\ ", r"\s+") for t in sorted(terms, key=len, reverse=True)]


_TERMS = load_redaction_terms()
COMPANY_TOKEN_RE = _word_regex(_literal_alternatives(_TERMS.get("company_ids", [])))
PLANT_TOKEN_RE = _word_regex(_TERMS.get("plant_patterns", []), re.IGNORECASE)
NAME_TOKEN_RE = _word_regex(_literal_alternatives(_TERMS.get("names", [])), re.IGNORECASE)
EMPLOYEE_ID_RE = (
    re.compile(r"(?<!\d)(" + "|".join(map(re.escape, _TERMS["employee_ids"])) + r")(?!\d)")
    if _TERMS.get("employee_ids") else None
)
USERNAME_RE = _word_regex(_literal_alternatives(_TERMS.get("usernames", [])), re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
URL_COMPANY_QUERY_RE = re.compile(r"([?&](?:company|companyid|curcomp)=)([^&#]+)", re.IGNORECASE)
URL_PLANT_QUERY_RE = re.compile(r"([?&](?:plant|plantid|curplant)=)([^&#]+)", re.IGNORECASE)
URL_USER_QUERY_RE = re.compile(r"([?&](?:user|userid|username|currentuserid)=)([^&#]+)", re.IGNORECASE)
URL_EMP_QUERY_RE = re.compile(r"([?&](?:employee|employeeid|employeenum|empid)=)([^&#]+)", re.IGNORECASE)
URL_USER_SEGMENT_RE = re.compile(r"(/(?:user|users|userid|username)/)([^/?#]+)", re.IGNORECASE)
URL_EMP_SEGMENT_RE = re.compile(r"(/(?:employee|employees|employeeid|empid)/)([^/?#]+)", re.IGNORECASE)
VERY_LONG_TOKEN_RE = re.compile(r"[A-Za-z._\-0-9]{1000,}")
CANONICAL_PLACEHOLDER_RE = re.compile(
    r"\{(INSTANCE|HOST|COMPANY_ID|PLANT_ID|NAME|EMPLOYEE_ID|USERNAME|TOKEN)\}",
    re.IGNORECASE,
)
BACKREF_WRAPPED_PLACEHOLDER_RE = re.compile(r"\$\d+\{([A-Za-z0-9_]+)\}\$\d+")
DEFAULT_CONTEXT_PAIR_WITH_COMMA_RE = re.compile(
    r'"(?:[^"\\]|\\.)*":(?:""|null|0|false),'
)
DEFAULT_CONTEXT_PAIR_TRAILING_RE = re.compile(
    r',"(?:[^"\\]|\\.)*":(?:""|null|0|false)(?=[}\]])'
)
JSON_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
JSON_MISSING_VALUE_RE = re.compile(r":\s*(?=[,}\]])")
JSON_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")

DEFAULT_DROP_NODE_NAME_PATTERNS = [
    "CheckForTaskUpdatesKeepIdleTime-*",
    "GetMenuID-*",
    "CheckMenuSecurityForUser-*",
    "GenXDatas-*",
    "GetNotificationsForCurrentUserKeepIdleTime-*",
]

DEFAULT_DROP_PARENT_NAME_PATTERNS = [
    "*/Ice.LIB.MetaFXSvc",
    "*/ICE.BO.MenuSvc",
    "*/Ice.BO.SysMonitorSvc",
    "*/Ice.BO.GenXDataSvc",
    "*/Ice.Lib.InAppNotificationsSvc",
    "*/Ice.BO.EdgeAgentConfigSvc",
    "*/Ice.lib.ClassAttributeSvc",
    "*/Ice.BO.FeatureUserSettingSvc",
]

AGGRESSIVE_DROP_PATH_PATTERNS = [
    "*.BOCall.request.url",
    "*.BOCall.request.queryParams.request",
    "*.BOCall.request.headers.Authorization",
    "*.BOCall.request.headers.Accept",
]

CONTEXT_ONLY_DROP_PATH_PATTERNS = [
    "*.BOCall.request.headers.Authorization",
]

CALL_CONTEXT_DROP_PATH_PATTERNS = [
    "*.BOCall.request.headers.contextheader",
    "*.BOCall.request.headers.SessionInfo",
    "*.BOCall.request.headers.callSettings",
    "*.BOCall.response.headers.contextheader",
    "*.BOCall.response.headers.callinfo",
]

EMPTY_STRINGIFIED_VALUES = {"", "{}", "[]", "null"}

DEFAULT_VALUE_REDACTION_BY_KEY: dict[str, str] = {
    "UserID": "{UserID}",
    "Name": "{Name}",
    "EmpID": "{EmpID}",
    "CompanyName": "{CompanyName}",
    "UserIDName": "{UserIDName}",
    "Company": "{COMPANY_ID}",
    "CurrentCompany": "{COMPANY_ID}",
    "CompanyID": "{COMPANY_ID}",
    "CurComp": "{COMPANY_ID}",
    "Plant": "{PLANT_ID}",
    "CurrentPlant": "{PLANT_ID}",
    "PlantID": "{PLANT_ID}",
    "CurPlant": "{PLANT_ID}",
    "PlantList": "{PLANT_LIST}",
    "CurrentUserId": "{USERNAME}",
    "Username": "{USERNAME}",
    "UserName": "{USERNAME}",
    "User": "{USERNAME}",
    "EmployeeNum": "{EMPLOYEE_ID}",
    "EmployeeID": "{EMPLOYEE_ID}",
    "EMailAddress": "{EMAIL}",
    "OfficePhone": "{PHONE}",
    "Phone": "{PHONE}",
    "Address1": "{ADDRESS}",
    "Address2": "{ADDRESS}",
    "City": "{CITY}",
    "State": "{STATE}",
    "ZIP": "{ZIP}",
    "Country": "{COUNTRY}",
    "WorkstationID": "{WORKSTATION_ID}",
}

# Built-in path presets tuned for Kinetic/Epicor trace payloads.
PRESETS: dict[str, list[str]] = {
    "kinetic-heavy": [
        "*.BOCall.request.headers.contextheader",
        "*.BOCall.response.headers.contextheader",
        "*.BOCall.response.headers.ClientHandler",
        "*.BOCall.request.headers.Authorization",
        "*.BOCall.request.headers.SessionInfo",
        "*.BOCall.request.headers.callSettings",
        "*.BOCall.request.headers.Accept",
        "*.BOCall.request.queryParams.request",
        "*.BOCall.request.url",
        "*.BOCall.response.body.returnObj.Layout.components",
        "*.BOCall.response.body.returnObj.Layout.model",
        "*.BOCall.response.body.returnObj.DataViews",
    ],
    "headers-only": [
        "*.request.headers.*",
        "*.response.headers.*",
    ],
}

DELTA_MODES = ["off", "summary", "annotated"]
DELTA_FORMATS = ["jsonl", "jsonc", "both"]


@dataclass
class Stats:
    removed: int = 0
    visited: int = 0
    normalized: int = 0
    sampled_lists: int = 0
    sampled_items: int = 0
    sampled_paths: list[tuple[str, int, int]] | None = None
    removed_by_rule: Counter[str] | None = None
    removed_paths: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.removed_by_rule = Counter()
        self.removed_paths = []
        self.sampled_paths = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove JSON paths from trace files quickly.\n"
            "Local, offline tool: shrinks large Kinetic trace/dump JSON files by dropping matching paths,\n"
            "keys and nodes, prunes noise, and redacts environment-identifying values (hosts, companies,\n"
            "plants, users, tokens). Writes reduced copies to --out-dir, or with --in-place overwrites the\n"
            "source files (a .bak backup is kept unless --no-backup)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.trim_trace_paths trace.json --preset kinetic-heavy --in-place\n"
            "  python -m kinetic_devops.trim_trace_paths \"*.json\" --drop \"*.request.headers.Authorization\" "
            "--out-dir reduced\n"
            "  python -m kinetic_devops.trim_trace_paths dump.json --delta-mode summary --dry-run"
        ),
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input files or glob patterns (examples: file.json, *.json, traces/**/*.json).",
    )
    parser.add_argument(
        "--drop",
        action="append",
        default=[],
        help="Path pattern to remove (can be repeated). Supports * wildcards.",
    )
    parser.add_argument(
        "--rules-file",
        action="append",
        default=[],
        help="Text file with one --drop pattern per line (# comments allowed).",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS.keys()),
        action="append",
        default=[],
        help="Built-in removal preset (can be repeated).",
    )
    parser.add_argument(
        "--drop-key",
        action="append",
        default=[],
        help="Drop this key name anywhere it appears (can be repeated).",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite each source file with its reduced version.",
    )
    parser.add_argument(
        "--out-dir",
        default="reduced",
        help="Output directory when not using --in-place (default: reduced).",
    )
    parser.add_argument(
        "--suffix",
        default="_reduced",
        help="Suffix added to output file names when not using --in-place.",
    )
    parser.add_argument(
        "--prune-empty",
        action="store_true",
        help="After removals, recursively drop empty objects/lists.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print output JSON (larger files, easier diffs).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files.",
    )
    parser.add_argument(
        "--show-samples",
        type=int,
        default=12,
        help="Number of removed path samples to print per file (default: 12).",
    )
    parser.add_argument(
        "--show-top-rules",
        type=int,
        default=10,
        help="Number of top matching rules to print per file (default: 10).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Disable automatic .bak file creation for --in-place writes.",
    )
    parser.add_argument(
        "--drop-node-name",
        action="append",
        default=[],
        help="Drop any node whose name matches this wildcard pattern (can be repeated).",
    )
    parser.add_argument(
        "--drop-parent-name",
        action="append",
        default=[],
        help="Drop entire service/root nodes whose name matches this wildcard pattern (can be repeated).",
    )
    parser.add_argument(
        "--no-default-prune",
        action="store_true",
        help="Disable built-in default node pruning rules.",
    )
    parser.add_argument(
        "--strip-empty-call-context",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Strip empty stringified call context values like '', '{}', '[]', 'null' (default: on).",
    )
    parser.add_argument(
        "--call-summary",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print summary of calls remaining after pruning (default: on).",
    )
    parser.add_argument(
        "--redact-mode",
        choices=["aggressive", "context-only"],
        default="aggressive",
        help="Redaction profile: aggressive (legacy default) or context-only.",
    )
    parser.add_argument(
        "--call-context-policy",
        choices=["purge", "keep", "purge-empty"],
        default="purge",
        help="How to handle call context fields: purge all, keep all, or purge only empty context values.",
    )
    parser.add_argument(
        "--sequence-only",
        action="store_true",
        help="Flatten surviving calls into one clean call sequence with resequenced sibling links.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=0,
        help="If greater than 0, keep only the first N items from each list encountered during cleanup.",
    )
    parser.add_argument(
        "--sample-include",
        action="append",
        default=[],
        help="Only sample lists whose path matches this wildcard pattern (can be repeated).",
    )
    parser.add_argument(
        "--sample-exclude",
        action="append",
        default=[],
        help="Never sample lists whose path matches this wildcard pattern (can be repeated).",
    )
    parser.add_argument(
        "--light-repair-json",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Attempt light JSON repair on parse failure (BOM/control chars, trailing commas, "
            "missing object values). Default: on."
        ),
    )
    parser.add_argument(
        "--delta-mode",
        choices=DELTA_MODES,
        default="off",
        help="Generate delta sidecar output alongside the reduced JSON (default: off).",
    )
    parser.add_argument(
        "--delta-format",
        choices=DELTA_FORMATS,
        default="jsonc",
        help="Delta sidecar format: jsonl, jsonc, or both (default: jsonc).",
    )
    parser.add_argument(
        "--delta-out-dir",
        default="delta",
        help="Output directory for delta sidecars (default: delta).",
    )
    parser.add_argument(
        "--delta-max-value-len",
        type=int,
        default=200,
        help="Maximum rendered value length when delta redaction is disabled (default: 200).",
    )
    parser.add_argument(
        "--delta-redact-values",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Redact delta old/new values by default; use --no-delta-redact-values to expose truncated values.",
    )
    return parser.parse_args()


def try_parse_json_with_light_repair(raw: str, enable_repair: bool) -> tuple[Any | None, str | None, str | None]:
    initial_error: str | None = None
    try:
        return json.loads(raw), None, None
    except Exception as exc:
        initial_error = str(exc)
        if not enable_repair:
            return None, None, initial_error

    candidates: list[tuple[str, str]] = []

    text = raw
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
        candidates.append((text, "bom"))

    no_ctrl = JSON_CONTROL_CHARS_RE.sub("", text)
    if no_ctrl != text:
        text = no_ctrl
        candidates.append((text, "ctrl-chars"))

    no_trailing_commas = JSON_TRAILING_COMMA_RE.sub(r"\1", text)
    if no_trailing_commas != text:
        text = no_trailing_commas
        candidates.append((text, "trailing-commas"))

    with_null_object_values = JSON_MISSING_VALUE_RE.sub(": null", text)
    if with_null_object_values != text:
        text = with_null_object_values
        candidates.append((text, "missing-values"))

    # Try each incremental repair candidate in sequence.
    seen: set[str] = set()
    last_err: str | None = None
    for candidate, note in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate), note, None
        except Exception as exc:
            last_err = str(exc)

    return None, None, last_err or initial_error or "unknown parse error"


def read_rules_file(path: str) -> list[str]:
    rules: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            rules.append(s)
    return rules


def expand_inputs(patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    for raw in patterns:
        matches = [Path(p) for p in glob.glob(raw, recursive=True)]
        if not matches:
            p = Path(raw)
            if p.exists():
                matches = [p]
        for p in matches:
            if p.is_file() and p not in seen:
                seen.add(p)
                paths.append(p)

    return sorted(paths)


def normalize_path(path: str) -> str:
    return INDEX_RE.sub("[*]", path)


def first_matching_pattern(path: str, patterns: list[str]) -> str | None:
    npath = normalize_path(path)
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(npath, pattern):
            return pattern
    return None


def record_removal(stats: Stats, path: str, rule: str) -> None:
    stats.removed += 1
    stats.removed_by_rule[rule] += 1
    stats.removed_paths.append((path, rule))


def normalize_trace_string(value: str) -> str:
    normalized = value
    normalized = BACKREF_WRAPPED_PLACEHOLDER_RE.sub(r"{\1}", normalized)
    normalized = EMAIL_RE.sub("{EMAIL}", normalized)
    normalized = ESCAPED_PLACEHOLDER_RE.sub(r"{\1}", normalized)
    normalized = LEGACY_BRACE_CLASS_ARTIFACT_RE.sub("", normalized)
    normalized = HOST_AND_INSTANCE_RE.sub("{HOST}.epicorsaas.com/{INSTANCE}", normalized)
    normalized = HOST_ONLY_RE.sub("{HOST}.epicorsaas.com", normalized)
    normalized = URL_COMPANY_QUERY_RE.sub(r"\1{COMPANY_ID}", normalized)
    normalized = URL_PLANT_QUERY_RE.sub(r"\1{PLANT_ID}", normalized)
    normalized = URL_USER_QUERY_RE.sub(r"\1{USERNAME}", normalized)
    normalized = URL_EMP_QUERY_RE.sub(r"\1{EMPLOYEE_ID}", normalized)
    normalized = URL_USER_SEGMENT_RE.sub(r"\1{USERNAME}", normalized)
    normalized = URL_EMP_SEGMENT_RE.sub(r"\1{EMPLOYEE_ID}", normalized)
    normalized = INSTANCE_TOKEN_RE.sub("{INSTANCE}", normalized)
    for pattern, placeholder in (
        (COMPANY_TOKEN_RE, "{COMPANY_ID}"),
        (PLANT_TOKEN_RE, "{PLANT_ID}"),
        (NAME_TOKEN_RE, "{NAME}"),
        (EMPLOYEE_ID_RE, "{EMPLOYEE_ID}"),
        (USERNAME_RE, "{USERNAME}"),
    ):
        if pattern is not None:
            normalized = pattern.sub(placeholder, normalized)
    normalized = VERY_LONG_TOKEN_RE.sub("{TOKEN}", normalized)
    normalized = CANONICAL_PLACEHOLDER_RE.sub(lambda m: "{" + m.group(1).upper() + "}", normalized)
    normalized = PLACEHOLDER_REPEAT_RE.sub(r"\1", normalized)
    return normalized


def normalize_placeholder_repeats(node: Any, path: str, stats: Stats) -> Any:
    if isinstance(node, str):
        normalized = normalize_trace_string(node)
        if normalized != node:
            stats.normalized += 1
        return normalized

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            normalized_key = normalize_trace_string(key) if isinstance(key, str) else key
            if normalized_key != key:
                stats.normalized += 1
            child_path = f"{path}.{normalized_key}" if path else str(normalized_key)
            out[normalized_key] = normalize_placeholder_repeats(value, child_path, stats)
        return out

    if isinstance(node, list):
        return [normalize_placeholder_repeats(value, f"{path}[{idx}]", stats) for idx, value in enumerate(node)]

    return node


def name_matches(name: str, patterns: list[str]) -> str | None:
    lower_name = name.lower()
    for pattern in patterns:
        if fnmatch.fnmatchcase(lower_name, pattern.lower()):
            return pattern
    return None


def relink_siblings(children: list[Any]) -> None:
    named_children: list[dict[str, Any]] = [
        c for c in children if isinstance(c, dict) and isinstance(c.get("name"), str)
    ]
    for i, child in enumerate(named_children):
        prev_name = named_children[i - 1]["name"] if i > 0 else None
        next_name = named_children[i + 1]["name"] if i < len(named_children) - 1 else None

        if prev_name is None:
            child.pop("prevSibling", None)
        else:
            child["prevSibling"] = prev_name

        if next_name is None:
            child.pop("nextSibling", None)
        else:
            child["nextSibling"] = next_name


def prune_trace_tree(
    node: Any,
    path: str,
    drop_node_name_patterns: list[str],
    drop_parent_name_patterns: list[str],
    stats: Stats,
) -> Any:
    if isinstance(node, dict):
        current_name = node.get("name") if isinstance(node.get("name"), str) else ""

        if "children" in node and isinstance(node["children"], list):
            filtered_children: list[Any] = []
            for idx, child in enumerate(node["children"]):
                child_name = child.get("name", "") if isinstance(child, dict) else ""
                child_path = f"{path}.children[{idx}]" if path else f"children[{idx}]"

                parent_rule = name_matches(child_name, drop_parent_name_patterns) if child_name else None
                if parent_rule:
                    record_removal(stats, f"{child_path}.name={child_name}", f"node:{parent_rule}")
                    continue

                node_rule = name_matches(child_name, drop_node_name_patterns) if child_name else None
                if node_rule:
                    record_removal(stats, f"{child_path}.name={child_name}", f"node:{node_rule}")
                    continue

                filtered_children.append(
                    prune_trace_tree(
                        child,
                        child_path,
                        drop_node_name_patterns,
                        drop_parent_name_patterns,
                        stats,
                    )
                )

            node["children"] = filtered_children
            relink_siblings(node["children"])

        for key, value in list(node.items()):
            if key == "children":
                continue
            child_path = f"{path}.{key}" if path else key
            node[key] = prune_trace_tree(
                value,
                child_path,
                drop_node_name_patterns,
                drop_parent_name_patterns,
                stats,
            )
        return node

    if isinstance(node, list):
        return [
            prune_trace_tree(v, f"{path}[{i}]", drop_node_name_patterns, drop_parent_name_patterns, stats)
            for i, v in enumerate(node)
        ]

    return node


def strip_empty_stringified_context(node: Any, path: str, stats: Stats) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(value, str) and value.strip().lower() in EMPTY_STRINGIFIED_VALUES:
                if should_strip_empty_value(child_path, stats):
                    record_removal(stats, child_path, "empty-stringified-context")
                    continue
            out[key] = strip_empty_stringified_context(value, child_path, stats)
        return out
    if isinstance(node, list):
        return [strip_empty_stringified_context(v, f"{path}[{i}]", stats) for i, v in enumerate(node)]
    return node


def should_strip_empty_value(path: str, stats: Stats) -> bool:
    context_paths = (
        "BOCall.request.headers.contextheader",
        "BOCall.request.headers.SessionInfo",
        "BOCall.request.headers.callSettings",
        "BOCall.request.headers.Authorization",
        "BOCall.response.headers.contextheader",
        "BOCall.response.headers.callinfo",
    )
    call_context_policy = getattr(stats, "call_context_policy", "purge")
    if call_context_policy == "keep":
        return False
    if call_context_policy == "purge-empty":
        return any(tag in path for tag in context_paths)

    mode = getattr(stats, "redact_mode", "aggressive")
    if mode == "context-only":
        return any(tag in path for tag in context_paths)

    return any(tag in path for tag in ("BOCall.request", "BOCall.response", "headers", "queryParams", "body", "context"))


def is_call_context_path(path: str) -> bool:
    tags = (
        "BOCall.request.headers.contextheader",
        "BOCall.request.headers.SessionInfo",
        "BOCall.request.headers.callSettings",
        "BOCall.response.headers.contextheader",
        "BOCall.response.headers.callinfo",
    )
    return any(tag in path for tag in tags)


def prune_default_scalar(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, str):
        return value == ""
    return False


def prune_default_values(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            cleaned = prune_default_values(value)
            if cleaned in ({}, []):
                continue
            if prune_default_scalar(cleaned):
                continue
            out[key] = cleaned
        return out

    if isinstance(node, list):
        out_list: list[Any] = []
        for value in node:
            cleaned = prune_default_values(value)
            if cleaned in ({}, []):
                continue
            if prune_default_scalar(cleaned):
                continue
            out_list.append(cleaned)
        return out_list

    return node


def context_key_token(key: str) -> str | None:
    k = key.lower()
    company_keys = {"company", "currentcompany", "companyid"}
    plant_keys = {"plant", "currentplant", "plantid"}
    user_keys = {"currentuserid", "userid", "username", "user_name", "user"}
    employee_keys = {"employeenum", "employeeid", "empid"}

    if k in company_keys:
        return "{COMPANY_ID}"
    if k in plant_keys:
        return "{PLANT_ID}"
    if k in user_keys:
        return "{USERNAME}"
    if k in employee_keys:
        return "{EMPLOYEE_ID}"
    return None


def normalize_context_identity_fields(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            token = context_key_token(key)
            if token is not None:
                out[key] = token
            else:
                out[key] = normalize_context_identity_fields(value)
        return out

    if isinstance(node, list):
        return [normalize_context_identity_fields(value) for value in node]

    return node


def normalize_context_identity_fields_in_text(text: str) -> str:
    patterns = [
        (re.compile(r'("(?:CurrentCompany|Company|CompanyID)"\s*:\s*")([^"]*)(")'), r'\1{COMPANY_ID}\3'),
        (re.compile(r'("(?:CurrentPlant|Plant|PlantID)"\s*:\s*")([^"]*)(")'), r'\1{PLANT_ID}\3'),
        (re.compile(r'("(?:CurrentUserId|UserID|UserName|Username|User)"\s*:\s*")([^"]*)(")'), r'\1{USERNAME}\3'),
        (re.compile(r'("(?:EmployeeNum|EmployeeID|EmpID)"\s*:\s*")([^"]*)(")'), r'\1{EMPLOYEE_ID}\3'),
    ]
    out = text
    for pattern, repl in patterns:
        out = pattern.sub(repl, out)

    # Handle escaped key/value forms inside stringified JSON (e.g. \"CurrentPlant\":\"...\").
    out = re.sub(r'(\\"CurrentCompany\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{COMPANY_ID}', out)
    out = re.sub(r'(\\"Company\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{COMPANY_ID}', out)
    out = re.sub(r'(\\"CurrentPlant\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{PLANT_ID}', out)
    out = re.sub(r'(\\"Plant\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{PLANT_ID}', out)
    out = re.sub(r'(\\"CurrentUserId\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{USERNAME}', out)
    out = re.sub(r'(\\"UserName\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{USERNAME}', out)
    out = re.sub(r'(\\"EmployeeNum\\":\\")[^\\"]*(?=\\"|[}\]])', r'\1{EMPLOYEE_ID}', out)

    # Recover malformed context pairs where value text leaks into the next key.
    out = re.sub(r'("CurrentCompany"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{COMPANY_ID}\",\2', out)
    out = re.sub(r'("Company"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{COMPANY_ID}\",\2', out)
    out = re.sub(r'("CurrentPlant"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{PLANT_ID}\",\2', out)
    out = re.sub(r'("Plant"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{PLANT_ID}\",\2', out)
    out = re.sub(r'("CurrentUserId"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{USERNAME}\",\2', out)
    out = re.sub(r'("UserName"\s*:\s*")[^"]*,("[A-Za-z_][A-Za-z0-9_]*"\s*:)', r'\1{USERNAME}\",\2', out)

    # Handle malformed terminal key/value pairs missing a closing quote before object/list end.
    out = re.sub(r'("CurrentCompany"\s*:\s*")[^"]*([}\]])', r'\1{COMPANY_ID}"\2', out)
    out = re.sub(r'("Company"\s*:\s*")[^"]*([}\]])', r'\1{COMPANY_ID}"\2', out)
    out = re.sub(r'("CurrentPlant"\s*:\s*")[^"]*([}\]])', r'\1{PLANT_ID}"\2', out)
    out = re.sub(r'("Plant"\s*:\s*")[^"]*([}\]])', r'\1{PLANT_ID}"\2', out)
    out = re.sub(r'("CurrentUserId"\s*:\s*")[^"]*([}\]])', r'\1{USERNAME}"\2', out)
    out = re.sub(r'("UserName"\s*:\s*")[^"]*([}\]])', r'\1{USERNAME}"\2', out)

    return out


def redact_values_by_key(node: Any, stats: Stats) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            replacement = DEFAULT_VALUE_REDACTION_BY_KEY.get(key)
            if replacement is not None:
                if value != replacement:
                    stats.normalized += 1
                out[key] = replacement
            else:
                out[key] = redact_values_by_key(value, stats)
        return out

    if isinstance(node, list):
        return [redact_values_by_key(value, stats) for value in node]

    return node


def compact_stringified_call_context(node: Any, path: str, stats: Stats) -> Any:
    policy = getattr(stats, "call_context_policy", "purge")
    if policy != "purge-empty":
        return node

    if isinstance(node, dict):
        return {
            key: compact_stringified_call_context(value, f"{path}.{key}" if path else key, stats)
            for key, value in node.items()
        }

    if isinstance(node, list):
        return [
            compact_stringified_call_context(value, f"{path}[{idx}]", stats)
            for idx, value in enumerate(node)
        ]

    if isinstance(node, str) and is_call_context_path(path):
        s = node.strip()
        if not s.startswith("{") and not s.startswith("["):
            return node
        try:
            parsed = json.loads(node)
        except Exception:
            cleaned_text = DEFAULT_CONTEXT_PAIR_WITH_COMMA_RE.sub("", node)
            cleaned_text = DEFAULT_CONTEXT_PAIR_TRAILING_RE.sub("", cleaned_text)
            cleaned_text = normalize_context_identity_fields_in_text(cleaned_text)
            if cleaned_text != node:
                stats.normalized += 1
            return cleaned_text
        cleaned = prune_default_values(parsed)
        cleaned = normalize_context_identity_fields(cleaned)
        if cleaned != parsed:
            stats.normalized += 1
            return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
    return node


def summarize_remaining_calls(node: Any) -> str:
    lines: list[str] = ["  remaining call summary:"]
    service_counts: Counter[str] = Counter()
    method_counts_by_service: dict[str, Counter[str]] = {}

    def walk(node: Any, current_service: str | None = None) -> None:
        if isinstance(node, dict):
            name = node.get("name")
            children = node.get("children")

            next_service = current_service
            if isinstance(name, str) and isinstance(children, list) and "BOCall" not in node:
                next_service = name

            if "BOCall" in node and isinstance(name, str):
                service_name = current_service
                if isinstance(node.get("serviceName"), str):
                    service_name = node["serviceName"]
                if not service_name:
                    service_name = "(unknown)"

                service_counts[service_name] += 1
                method = name.rsplit("-", 1)[0] if "-" in name else name
                method_counts_by_service.setdefault(service_name, Counter())[method] += 1

            if isinstance(children, list):
                for child in children:
                    walk(child, next_service)
        elif isinstance(node, list):
            for child in node:
                walk(child, current_service)

    walk(node)

    if not service_counts:
        lines.append("    (no remaining calls)")
        return "\n".join(lines)

    for service_name, total_calls in service_counts.most_common():
        top_methods = ", ".join(
            [f"{m}({n})" for m, n in method_counts_by_service[service_name].most_common(5)]
        )
        lines.append(f"    - {service_name} | calls={total_calls} | top={top_methods}")

    return "\n".join(lines)


def flatten_call_sequence(root: Any, stats: Stats) -> Any:
    if not isinstance(root, dict):
        return root

    sequence: list[dict[str, Any]] = []

    def walk(node: Any, service_name: str | None = None) -> None:
        if isinstance(node, dict):
            name = node.get("name")
            children = node.get("children")

            next_service = service_name
            if isinstance(name, str) and isinstance(children, list) and "BOCall" not in node:
                next_service = name

            if "BOCall" in node and isinstance(name, str):
                cloned = copy.deepcopy(node)
                if next_service:
                    cloned["serviceName"] = next_service
                sequence.append(cloned)

            if isinstance(children, list):
                for child in children:
                    walk(child, next_service)
        elif isinstance(node, list):
            for child in node:
                walk(child, service_name)

    walk(root)

    sequence.sort(
        key=lambda call: (
            call.get("timestamp") if isinstance(call.get("timestamp"), (int, float)) else float("inf"),
            call.get("serviceName", ""),
            call.get("name", ""),
        )
    )

    for index, call in enumerate(sequence):
        call["sequenceIndex"] = index + 1
        if index == 0:
            call.pop("prevSibling", None)
        else:
            call["prevSibling"] = sequence[index - 1].get("name")

        if index == len(sequence) - 1:
            call.pop("nextSibling", None)
        else:
            call["nextSibling"] = sequence[index + 1].get("name")

    stats.normalized += 0
    return {"children": sequence, "sequenceOnly": True}


def reorder_context_headers(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        normalized_path = normalize_path(path)
        is_bocall_headers = (
            normalized_path.endswith(".BOCall.request.headers")
            or normalized_path.endswith(".BOCall.response.headers")
            or normalized_path == "BOCall.request.headers"
            or normalized_path == "BOCall.response.headers"
        )

        if is_bocall_headers:
            # Keep normal header keys first and push large/noisy context blobs to the tail.
            tail_order = ["contextheader", "SessionInfo", "callSettings", "callinfo"]
            reordered: dict[str, Any] = {}

            for key, value in node.items():
                if key not in tail_order:
                    child_path = f"{path}.{key}" if path else key
                    reordered[key] = reorder_context_headers(value, child_path)

            for key in tail_order:
                if key in node:
                    child_path = f"{path}.{key}" if path else key
                    reordered[key] = reorder_context_headers(node[key], child_path)

            return reordered

        return {
            key: reorder_context_headers(value, f"{path}.{key}" if path else key)
            for key, value in node.items()
        }

    if isinstance(node, list):
        return [reorder_context_headers(value, f"{path}[{idx}]") for idx, value in enumerate(node)]

    return node


def trim_node(
    node: Any,
    path: str,
    drop_patterns: list[str],
    drop_keys: set[str],
    stats: Stats,
) -> Any:
    stats.visited += 1

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key
            if key in drop_keys:
                record_removal(stats, child_path, f"key:{key}")
                continue
            matched = first_matching_pattern(child_path, drop_patterns)
            if matched is not None:
                record_removal(stats, child_path, matched)
                continue
            out[key] = trim_node(value, child_path, drop_patterns, drop_keys, stats)
        return out

    if isinstance(node, list):
        out_list: list[Any] = []
        for idx, value in enumerate(node):
            child_path = f"{path}[{idx}]"
            matched = first_matching_pattern(child_path, drop_patterns)
            if matched is not None:
                record_removal(stats, child_path, matched)
                continue
            out_list.append(trim_node(value, child_path, drop_patterns, drop_keys, stats))
        return out_list

    return node


def prune_empty(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned = {k: prune_empty(v) for k, v in node.items()}
        return {k: v for k, v in cleaned.items() if v not in ({}, [])}
    if isinstance(node, list):
        cleaned_list = [prune_empty(v) for v in node]
        return [v for v in cleaned_list if v not in ({}, [])]
    return node


def list_path_matches(path: str, patterns: list[str]) -> bool:
    normalized = normalize_path(path)
    return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


def should_sample_list(path: str, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    if exclude_patterns and list_path_matches(path, exclude_patterns):
        return False
    if include_patterns:
        return list_path_matches(path, include_patterns)
    return True


def sample_lists(
    node: Any,
    path: str,
    sample_size: int,
    stats: Stats,
    include_patterns: list[str],
    exclude_patterns: list[str],
) -> Any:
    if sample_size <= 0:
        return node

    if isinstance(node, dict):
        return {
            key: sample_lists(
                value,
                f"{path}.{key}" if path else key,
                sample_size,
                stats,
                include_patterns,
                exclude_patterns,
            )
            for key, value in node.items()
        }

    if isinstance(node, list):
        if should_sample_list(path, include_patterns, exclude_patterns):
            sampled = node[:sample_size]
        else:
            sampled = node

        if len(node) > len(sampled):
            stats.sampled_lists += 1
            stats.sampled_items += len(node) - len(sampled)
            stats.sampled_paths.append((path or "(root-list)", len(node), len(sampled)))
        return [
            sample_lists(
                value,
                f"{path}[{idx}]",
                sample_size,
                stats,
                include_patterns,
                exclude_patterns,
            )
            for idx, value in enumerate(sampled)
        ]

    return node


def target_path(src: Path, in_place: bool, out_dir: str, suffix: str) -> Path:
    if in_place:
        return src
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    return out / f"{src.stem}{suffix}{src.suffix}"


def make_backup(src: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = src.with_name(f"{src.name}.{timestamp}.bak")
    i = 1
    while candidate.exists():
        candidate = src.with_name(f"{src.name}.{timestamp}.{i}.bak")
        i += 1
    shutil.copy2(src, candidate)
    return candidate


def format_transparency(stats: Stats, show_samples: int, show_top_rules: int) -> str:
    lines: list[str] = []
    lines.append("  top rules:")
    for rule, count in stats.removed_by_rule.most_common(max(show_top_rules, 0)):
        lines.append(f"    {count:>6}  {rule}")
    if not stats.removed_by_rule:
        lines.append("      (none)")

    if show_samples > 0:
        lines.append("  sample removed paths:")
        for path, rule in stats.removed_paths[:show_samples]:
            lines.append(f"    - {path}  [{rule}]")
        if not stats.removed_paths:
            lines.append("    (none)")

    if stats.sampled_paths:
        lines.append("  sampled lists:")
        for path, original_count, kept_count in stats.sampled_paths[:show_samples or len(stats.sampled_paths)]:
            lines.append(f"    - {path}  [{original_count} -> {kept_count}]")

    return "\n".join(lines)


def _delta_relative_dir(src: Path) -> Path:
    parts = src.parts[1:] if src.is_absolute() else src.parts
    if len(parts) <= 1:
        return Path()
    return Path(*parts[:-1])


def _delta_output_paths(src: Path, delta_out_dir: str, delta_mode: str, delta_format: str) -> list[Path]:
    base_dir = Path(delta_out_dir) / _delta_relative_dir(src)
    base_name = f"{src.stem}.delta.{delta_mode}"
    formats = ["jsonl", "jsonc"] if delta_format == "both" else [delta_format]
    return [base_dir / f"{base_name}.{fmt}" for fmt in formats]


def _delta_join_path(parent: str, segment: str) -> str:
    if not parent:
        return segment
    if segment.startswith("["):
        return f"{parent}{segment}"
    return f"{parent}.{segment}"


def _delta_format_scalar(value: Any, redact_values: bool, max_len: int) -> Any:
    if value is None or isinstance(value, bool):
        return value

    if redact_values:
        if isinstance(value, (int, float)):
            return "[REDACTED_NUMBER]"
        if isinstance(value, str):
            return "[REDACTED]"
        if isinstance(value, dict):
            return f"[REDACTED_OBJECT keys={len(value)}]"
        if isinstance(value, list):
            return f"[REDACTED_ARRAY items={len(value)}]"
        return "[REDACTED]"

    if isinstance(value, str):
        if len(value) <= max_len:
            return value
        return value[:max_len] + "...[truncated]"

    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        if len(rendered) <= max_len:
            return rendered
        return rendered[:max_len] + "...[truncated]"

    return value


def _delta_rule_for_path(path: str, stats: Stats) -> str | None:
    best_rule: str | None = None
    best_len = -1
    for recorded_path, rule in stats.removed_paths or []:
        if recorded_path == path or recorded_path.startswith(path) or path.startswith(recorded_path):
            candidate_len = len(recorded_path)
            if candidate_len > best_len:
                best_len = candidate_len
                best_rule = rule
    return best_rule


def _delta_emit_event(
    events: list[dict[str, Any]],
    op: str,
    path: str,
    old: Any,
    new: Any,
    stats: Stats,
    redact_values: bool,
    max_value_len: int,
) -> None:
    event: dict[str, Any] = {
        "op": op,
        "path": path or "(root)",
        "old": _delta_format_scalar(old, redact_values, max_value_len) if op in {"remove", "modify"} else None,
        "new": _delta_format_scalar(new, redact_values, max_value_len) if op in {"add", "modify"} else None,
    }
    rule = _delta_rule_for_path(path, stats)
    if rule is not None:
        event["rule"] = rule
    events.append(event)


def build_delta_events(
    before: Any,
    after: Any,
    stats: Stats,
    redact_values: bool,
    max_value_len: int,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def walk(old: Any, new: Any, path: str) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                child_path = _delta_join_path(path, str(key))
                if key not in new:
                    _delta_emit_event(events, "remove", child_path, old[key], None, stats, redact_values, max_value_len)
                    continue
                if key not in old:
                    _delta_emit_event(events, "add", child_path, None, new[key], stats, redact_values, max_value_len)
                    continue
                walk(old[key], new[key], child_path)
            return

        if isinstance(old, list) and isinstance(new, list):
            limit = max(len(old), len(new))
            for idx in range(limit):
                child_path = f"{path}[{idx}]" if path else f"[{idx}]"
                if idx >= len(new):
                    _delta_emit_event(events, "remove", child_path, old[idx], None, stats, redact_values, max_value_len)
                    continue
                if idx >= len(old):
                    _delta_emit_event(events, "add", child_path, None, new[idx], stats, redact_values, max_value_len)
                    continue
                walk(old[idx], new[idx], child_path)
            return

        if old != new:
            _delta_emit_event(events, "modify", path, old, new, stats, redact_values, max_value_len)

    walk(before, after, "")
    return events


def build_delta_summary(events: list[dict[str, Any]], src: Path, dst: Path, delta_mode: str) -> dict[str, Any]:
    op_counts = Counter(event["op"] for event in events)
    path_counts: Counter[str] = Counter()
    for event in events:
        path_counts[str(event.get("path") or "(root)")] += 1

    top_changed_paths = [
        {"path": path, "count": count}
        for path, count in sorted(path_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]

    return {
        "type": "delta-summary",
        "mode": delta_mode,
        "source": str(src),
        "target": str(dst),
        "added": op_counts.get("add", 0),
        "removed": op_counts.get("remove", 0),
        "modified": op_counts.get("modify", 0),
        "top_changed_paths": top_changed_paths,
    }


def _jsonc_value(value: Any, indent: int = 0) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        child_indent = " " * (indent + 2)
        inner = ",\n".join(f"{child_indent}{_jsonc_value(item, indent + 2)}" for item in value)
        return f"[\n{inner}\n{' ' * indent}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        child_indent = " " * (indent + 2)
        parts = []
        for key in sorted(value):
            parts.append(f"{child_indent}{json.dumps(str(key), ensure_ascii=False)}: {_jsonc_value(value[key], indent + 2)}")
        joined_parts = ",\n".join(parts)
        return f"{{\n{joined_parts}\n{' ' * indent}}}"
    return json.dumps(str(value), ensure_ascii=False)


def render_delta_jsonc(summary: dict[str, Any], events: list[dict[str, Any]], delta_mode: str) -> str:
    lines: list[str] = [
        f"// trace delta sidecar ({delta_mode})",
        f"// source: {summary.get('source', '')}",
        f"// target: {summary.get('target', '')}",
    ]
    if delta_mode == "summary":
        payload = dict(summary)
    else:
        payload = dict(summary)
        payload["events"] = events
        lines.append(f"// events: {len(events)}")
        for event in events:
            lines.append(f"// {event['op']} {event['path']}")
            if event.get("rule"):
                lines.append(f"// rule: {event['rule']}")
            if event.get("old") is not None:
                lines.append(f"// old: {_jsonc_value(event['old'])}")
            if event.get("new") is not None:
                lines.append(f"// new: {_jsonc_value(event['new'])}")
    lines.append(_jsonc_value(payload, 0))
    return "\n".join(lines) + "\n"


def render_delta_jsonl(summary: dict[str, Any], events: list[dict[str, Any]], delta_mode: str) -> str:
    lines = [json.dumps(summary, ensure_ascii=False, sort_keys=True)]
    if delta_mode == "annotated":
        lines.extend(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events)
    return "\n".join(lines) + "\n"


def write_delta_sidecars(
    src: Path,
    dst: Path,
    before: Any,
    after: Any,
    stats: Stats,
    delta_mode: str,
    delta_format: str,
    delta_out_dir: str,
    delta_redact_values: bool,
    delta_max_value_len: int,
    dry_run: bool,
) -> list[Path]:
    if delta_mode == "off":
        return []

    events = build_delta_events(before, after, stats, delta_redact_values, delta_max_value_len)
    summary = build_delta_summary(events, src, dst, delta_mode)
    output_paths = _delta_output_paths(src, delta_out_dir, delta_mode, delta_format)
    rendered_outputs: dict[str, str] = {}

    if delta_format in {"jsonl", "both"}:
        rendered_outputs["jsonl"] = render_delta_jsonl(summary, events, delta_mode)
    if delta_format in {"jsonc", "both"}:
        rendered_outputs["jsonc"] = render_delta_jsonc(summary, events, delta_mode)

    written_paths: list[Path] = []
    for output_path in output_paths:
        out_format = output_path.suffix.lstrip(".")
        content = rendered_outputs.get(out_format)
        if content is None:
            continue
        if not dry_run:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")
        written_paths.append(output_path)

    return written_paths


def process_file(
    src: Path,
    drop_patterns: list[str],
    drop_keys: set[str],
    in_place: bool,
    out_dir: str,
    suffix: str,
    pretty: bool,
    dry_run: bool,
    do_prune_empty: bool,
    show_samples: int,
    show_top_rules: int,
    no_backup: bool,
    drop_node_name_patterns: list[str],
    drop_parent_name_patterns: list[str],
    strip_empty_call_context: bool,
    show_call_summary: bool,
    sequence_only: bool,
    redact_mode: str,
    call_context_policy: str,
    light_repair_json: bool,
    sample_size: int,
    sample_include_patterns: list[str],
    sample_exclude_patterns: list[str],
    delta_mode: str,
    delta_format: str,
    delta_out_dir: str,
    delta_max_value_len: int,
    delta_redact_values: bool,
) -> tuple[bool, str, str]:
    try:
        raw = src.read_text(encoding="utf-8")
    except Exception as exc:
        return False, f"ERROR {src}: failed to read file ({exc})", ""

    data, repair_note, parse_error = try_parse_json_with_light_repair(raw, enable_repair=light_repair_json)
    if parse_error is not None or data is None:
        return False, f"ERROR {src}: failed to parse JSON ({parse_error})", ""

    stats = Stats()
    setattr(stats, "redact_mode", redact_mode)
    setattr(stats, "call_context_policy", call_context_policy)
    original_data = copy.deepcopy(data) if delta_mode != "off" else None
    data = prune_trace_tree(
        data,
        "",
        drop_node_name_patterns=drop_node_name_patterns,
        drop_parent_name_patterns=drop_parent_name_patterns,
        stats=stats,
    )
    trimmed = trim_node(data, "", drop_patterns, drop_keys, stats)
    trimmed = normalize_placeholder_repeats(trimmed, "", stats)
    trimmed = compact_stringified_call_context(trimmed, "", stats)
    trimmed = redact_values_by_key(trimmed, stats)
    if strip_empty_call_context:
        trimmed = strip_empty_stringified_context(trimmed, "", stats)
    trimmed = reorder_context_headers(trimmed)
    if do_prune_empty:
        trimmed = prune_empty(trimmed)
    if sequence_only:
        trimmed = flatten_call_sequence(trimmed, stats)
        if do_prune_empty:
            trimmed = prune_empty(trimmed)
    if sample_size > 0:
        trimmed = sample_lists(
            trimmed,
            "",
            sample_size,
            stats,
            sample_include_patterns,
            sample_exclude_patterns,
        )

    if pretty:
        out_text = json.dumps(trimmed, ensure_ascii=False, indent=2)
    else:
        out_text = json.dumps(trimmed, ensure_ascii=False, separators=(",", ":"))

    dst = target_path(src, in_place, out_dir, suffix)
    delta_paths: list[Path] = []
    if delta_mode != "off" and original_data is not None:
        delta_paths = write_delta_sidecars(
            src=src,
            dst=dst,
            before=original_data,
            after=trimmed,
            stats=stats,
            delta_mode=delta_mode,
            delta_format=delta_format,
            delta_out_dir=delta_out_dir,
            delta_redact_values=delta_redact_values,
            delta_max_value_len=delta_max_value_len,
            dry_run=dry_run,
        )

    before = len(raw.encode("utf-8"))
    after = len(out_text.encode("utf-8"))
    pct = 0.0 if before == 0 else (1.0 - (after / before)) * 100.0
    backup_msg = ""

    if not dry_run:
        if in_place and not no_backup:
            backup = make_backup(src)
            backup_msg = f" | backup={backup}"
        dst.write_text(out_text, encoding="utf-8")
        where = str(dst)
    else:
        where = "(dry-run)"

    summary = (
        f"OK {src} -> {where} | removed={stats.removed} nodes | "
        f"normalized={stats.normalized} strings | size {before:,} -> {after:,} bytes ({pct:.1f}% smaller)"
    )
    if sample_size > 0:
        summary += f" | sampled={stats.sampled_lists} lists / {stats.sampled_items} items (first {sample_size})"
        if sample_include_patterns:
            summary += f" | sample-include={len(sample_include_patterns)}"
        if sample_exclude_patterns:
            summary += f" | sample-exclude={len(sample_exclude_patterns)}"
    if delta_mode != "off":
        delta_desc = ", ".join(str(path) for path in delta_paths) if delta_paths else "(dry-run)"
        summary += f" | delta={delta_mode}:{delta_format} -> {delta_desc}"
    if repair_note:
        summary += f" | repaired-json={repair_note}"
    summary += backup_msg
    details = format_transparency(stats, show_samples=show_samples, show_top_rules=show_top_rules)
    if show_call_summary:
        call_summary = summarize_remaining_calls(trimmed)
        if call_summary:
            details = f"{details}\n{call_summary}" if details else call_summary
    return True, summary, details


def main() -> int:
    args = parse_args()

    drop_patterns: list[str] = list(args.drop)
    for preset in args.preset:
        drop_patterns.extend(PRESETS[preset])
    for rf in args.rules_file:
        drop_patterns.extend(read_rules_file(rf))

    drop_keys = set(args.drop_key)

    drop_node_name_patterns = list(args.drop_node_name)
    drop_parent_name_patterns = list(args.drop_parent_name)
    if not args.no_default_prune:
        if args.redact_mode == "context-only":
            drop_patterns.extend(CONTEXT_ONLY_DROP_PATH_PATTERNS)
        else:
            drop_patterns.extend(AGGRESSIVE_DROP_PATH_PATTERNS)
        if args.call_context_policy == "purge":
            drop_patterns.extend(CALL_CONTEXT_DROP_PATH_PATTERNS)
        drop_node_name_patterns.extend(DEFAULT_DROP_NODE_NAME_PATTERNS)
        drop_parent_name_patterns.extend(DEFAULT_DROP_PARENT_NAME_PATTERNS)

    if not drop_patterns and not drop_keys and not drop_node_name_patterns and not drop_parent_name_patterns:
        print(
            "Nothing to do: provide pruning options or leave defaults enabled.",
            file=sys.stderr,
        )
        return 2

    files = expand_inputs(args.inputs)
    if not files:
        print("No files matched input patterns.", file=sys.stderr)
        return 2

    failures = 0

    print("Active drop patterns:")
    for p in drop_patterns:
        print(f"  - {p}")
    if drop_keys:
        print("Active drop keys:")
        for k in sorted(drop_keys):
            print(f"  - {k}")
    if drop_parent_name_patterns:
        print("Active drop parent-name patterns:")
        for p in drop_parent_name_patterns:
            print(f"  - {p}")
    if drop_node_name_patterns:
        print("Active drop node-name patterns:")
        for p in drop_node_name_patterns:
            print(f"  - {p}")

    for src in files:
        ok, msg, detail = process_file(
            src=src,
            drop_patterns=drop_patterns,
            drop_keys=drop_keys,
            in_place=args.in_place,
            out_dir=args.out_dir,
            suffix=args.suffix,
            pretty=args.pretty,
            dry_run=args.dry_run,
            do_prune_empty=args.prune_empty,
            show_samples=args.show_samples,
            show_top_rules=args.show_top_rules,
            no_backup=args.no_backup,
            drop_node_name_patterns=drop_node_name_patterns,
            drop_parent_name_patterns=drop_parent_name_patterns,
            strip_empty_call_context=args.strip_empty_call_context,
            show_call_summary=args.call_summary,
            sequence_only=args.sequence_only,
            redact_mode=args.redact_mode,
            call_context_policy=args.call_context_policy,
            light_repair_json=args.light_repair_json,
            sample_size=args.sample_size,
            sample_include_patterns=args.sample_include,
            sample_exclude_patterns=args.sample_exclude,
            delta_mode=args.delta_mode,
            delta_format=args.delta_format,
            delta_out_dir=args.delta_out_dir,
            delta_max_value_len=args.delta_max_value_len,
            delta_redact_values=args.delta_redact_values,
        )
        if detail:
            print(detail)
        print(msg)
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
