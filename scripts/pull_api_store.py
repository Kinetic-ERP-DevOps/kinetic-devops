#!/usr/bin/env python3
"""Pull Epicor Kinetic Swagger/OData/method specs into a local API store.

Downloads discovered .json and .yaml specs from:
- /api/swagger/v2/odata
- /api/swagger/v2/methods
- /api/swagger/v2/odata/index.html
- /api/help/v2/index.html

Usage:
  python scripts/pull_api_store.py
    python scripts/pull_api_store.py --env <ENV> --user <USER>
    python scripts/pull_api_store.py --out-dir projects/Kinetic-API-Store --format json
    python scripts/pull_api_store.py --surface odata
    python scripts/pull_api_store.py --surface methods --service <SERVICE>
    python scripts/pull_api_store.py --missing-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kinetic_devops.auth import KineticConfigManager


SPEC_URL_RE = re.compile(r"https?://[^\s\"'<>]+/api/swagger/v2/(?:odata|methods)/[^\s\"'<>]+\.(?:json|yaml)", re.IGNORECASE)
REL_LINK_RE = re.compile(r"href=[\"']([^\"']+\.(?:json|yaml))[\"']", re.IGNORECASE)
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+\.(?:BO|Proc|Lib|Rpt|UD)\.[A-Za-z0-9_]+Svc$", re.IGNORECASE)
SWAGGER_REL_RE = re.compile(r"/api/swagger/v2/(?:odata|methods)/[^\s\"'<>]+\.(?:json|yaml)", re.IGNORECASE)


def resolve_active_config(env: str | None, user: str | None, company: str | None):
    mgr = KineticConfigManager()

    if env:
        context = (env, user, company)
    else:
        env_name, user_id, company_id = mgr.prompt_for_env()
        if user:
            user_id = user
        if company:
            company_id = company
        context = (env_name, user_id, company_id)

    url, token, api_key, active_company, nickname, active_user = mgr.get_active_config(
        context,
        fields=("url", "token", "api_key", "company", "nickname", "user_id"),
    )

    if not url or not token or not api_key or not active_company:
        print("❌ Could not resolve active Kinetic config/token for API pull.")
        sys.exit(1)

    return mgr, {
        "url": url,
        "token": token,
        "api_key": api_key,
        "company": company or active_company,
        "nickname": nickname,
        "user_id": active_user,
    }


def sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


def build_headers(mgr: KineticConfigManager, config: dict) -> dict:
    headers = mgr.get_auth_headers(config)
    headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "x-epi-request-etag": "true",
            "x-epi-extension-serialization": "full-metadata",
        }
    )
    return headers


def redact_headers(headers: dict) -> dict:
    sensitive = {"authorization", "x-api-key", "cookie", "password", "set-cookie"}
    redacted = {}
    for k, v in headers.items():
        if k.lower() in sensitive:
            redacted[k] = "[REDACTED]"
        else:
            redacted[k] = v
    return redacted


def derive_instance_root(url: str) -> str:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    parts = [p for p in parsed.path.split("/") if p]
    if parts:
        return f"{origin}/{parts[0]}"
    return origin


def iter_strings(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from iter_strings(v)
    elif isinstance(node, list):
        for item in node:
            yield from iter_strings(item)
    elif isinstance(node, str):
        yield node


def extract_service_names(payload) -> set[str]:
    names: set[str] = set()
    for val in iter_strings(payload):
        text = val.strip()
        if SERVICE_NAME_RE.match(text):
            names.add(text)
    return names


def extract_swagger_links(text: str, endpoint: str, instance_root: str) -> set[str]:
    found: set[str] = set()
    for m in SPEC_URL_RE.findall(text or ""):
        found.add(m)
    for rel in REL_LINK_RE.findall(text or ""):
        abs_url = urljoin(endpoint, rel)
        if "/api/swagger/v2/odata/" in abs_url.lower() or "/api/swagger/v2/methods/" in abs_url.lower():
            found.add(abs_url)
    for rel_path in SWAGGER_REL_RE.findall(text or ""):
        found.add(urljoin(instance_root + "/", rel_path.lstrip("/")))
    return found


def classify_surface(url: str) -> str:
    lower = url.lower()
    if "/api/swagger/v2/methods/" in lower:
        return "methods"
    return "odata"


def classify_service_type(url: str) -> str:
    name = os.path.splitext(os.path.basename(urlparse(url).path))[0]
    parts = name.split('.')
    return parts[1] if len(parts) >= 3 else ""


def classify_service_name(url: str) -> str:
    return os.path.splitext(os.path.basename(urlparse(url).path))[0]


def extract_version_info_from_bytes(content: bytes, url: str) -> dict | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    info = payload.get("info") or {}
    return {
        "title": info.get("title", classify_service_name(url)),
        "version": info.get("version", ""),
        "openapi": payload.get("openapi") or payload.get("swagger", ""),
        "surface": classify_surface(url),
        "service_type": classify_service_type(url),
        "service_name": classify_service_name(url),
    }


def probe_server_version(instance_root: str, headers: dict, timeout: int) -> dict:
    candidates = [
        f"{instance_root}/api/help/v2/index.html",
        f"{instance_root}/api/helppage/services?serviceType=BO",
    ]
    header_candidates = [
        "X-Appserver-Version",
        "X-Kinetic-Version",
        "X-Version",
        "ServerVersion",
        "ProductVersion",
        "Release",
    ]
    body_patterns = [
        re.compile(r'"version"\s*:\s*"([^"]+)"', re.IGNORECASE),
        re.compile(r'\bversion\s*[:=]\s*([A-Za-z0-9._-]+)', re.IGNORECASE),
        re.compile(r'\b(build|release)\s*[:=]\s*([A-Za-z0-9._-]+)', re.IGNORECASE),
    ]

    for endpoint in candidates:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=timeout)
        except Exception:
            continue

        if not resp.ok:
            continue

        for key in header_candidates:
            value = str(resp.headers.get(key, "") or "").strip()
            if value:
                return {"source": endpoint, "kind": "header", "name": key, "value": value}

        text = resp.text or ""
        for pattern in body_patterns:
            match = pattern.search(text)
            if not match:
                continue
            value = match.group(match.lastindex or 1).strip()
            if value:
                return {"source": endpoint, "kind": "body", "name": pattern.pattern, "value": value}

    return {}


def normalize_major_version(version_text: str) -> str:
    """Extract a major Kinetic version token like 2025.1 from arbitrary text."""
    if not version_text:
        return ""
    m = re.search(r"\b(20\d{2}\.\d+)\b", version_text)
    return m.group(1) if m else ""


def fingerprint_text(value: str) -> str:
    text = str(value or "").encode("utf-8")
    return hashlib.sha256(text).hexdigest()[:16]


def redact_free_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"https?://\S+", "[URL]", text, flags=re.IGNORECASE)
    text = re.sub(r"[A-Za-z0-9_]+\.(?:BO|Proc|Lib|Rpt|UD)\.[A-Za-z0-9_]+Svc", "[SERVICE]", text)
    text = re.sub(r"\bT_\d+\b", "[TENANT]", text)
    return text


def discovery_category(endpoint: str) -> str:
    e = endpoint.lower()
    if "servicetype=bo" in e:
        return "services_bo"
    if "servicetype=proc" in e:
        return "services_proc"
    if "servicetype=lib" in e:
        return "services_lib"
    if "servicetype=rpt" in e:
        return "services_rpt"
    if "servicetype=ud" in e:
        return "services_ud"
    if "/api/swagger/v2/odata" in e:
        return "swagger_odata"
    if "/api/swagger/v2/methods" in e:
        return "swagger_methods"
    if "/api/help/v2/index.html" in e:
        return "help_v2_index"
    return "other"


def summarize_discovery(discovery_log: list[dict]) -> list[dict]:
    bucket: dict[str, dict] = {}
    for item in discovery_log:
        cat = discovery_category(str(item.get("endpoint", "")))
        row = bucket.setdefault(cat, {"category": cat, "attempts": 0, "successes": 0, "status_codes": []})
        row["attempts"] += 1
        if item.get("ok"):
            row["successes"] += 1
        status = item.get("status")
        if status is not None and status not in row["status_codes"]:
            row["status_codes"].append(status)
    return sorted(bucket.values(), key=lambda x: x["category"])


def load_version_history_yaml(path: Path) -> list[dict]:
    if not path.exists():
        return []

    entries: list[dict] = []
    current: dict | None = None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.rstrip()
            if not line.strip() or line.strip().startswith("#"):
                continue
            if line.startswith("- "):
                if current:
                    entries.append(current)
                current = {}
                line = line[2:]
                if ":" in line:
                    k, v = line.split(":", 1)
                    current[k.strip()] = v.strip().strip('"')
                continue
            if current is None:
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                current[k.strip()] = v.strip().strip('"')
        if current:
            entries.append(current)
    except Exception:
        return []
    return entries


def write_version_history_yaml(path: Path, entries: list[dict]) -> None:
    lines = [
        "# Kinetic API pull history",
        "# This file is intended for git history review by connection version.",
    ]
    for item in entries:
        lines.append(f"- pulled_at_utc: \"{item.get('pulled_at_utc', '')}\"")
        lines.append(f"  connection_fingerprint: \"{item.get('connection_fingerprint', '')}\"")
        lines.append(f"  connection_version: \"{item.get('connection_version', '')}\"")
        lines.append(f"  major_version: \"{item.get('major_version', '')}\"")
        lines.append(f"  requested_surface: \"{item.get('requested_surface', '')}\"")
        lines.append(f"  requested_format: \"{item.get('requested_format', '')}\"")
        lines.append(f"  downloaded_count: \"{item.get('downloaded_count', 0)}\"")
        lines.append(f"  updated_count: \"{item.get('updated_count', 0)}\"")
        lines.append(f"  unchanged_count: \"{item.get('unchanged_count', 0)}\"")
        lines.append(f"  skipped_count: \"{item.get('skipped_count', 0)}\"")
        lines.append(f"  failed_count: \"{item.get('failed_count', 0)}\"")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def major_version_already_pulled(entries: list[dict], connection_fingerprint: str, major_version: str) -> bool:
    if not connection_fingerprint or not major_version:
        return False
    for item in entries:
        if (
            str(item.get("connection_fingerprint", "")) == connection_fingerprint
            and str(item.get("major_version", "")) == major_version
        ):
            return True
    return False


def discover_spec_urls(instance_root: str, headers: dict, timeout: int) -> tuple[list[str], list[dict]]:
    service_types = ["BO", "Proc", "Lib", "Rpt", "UD"]
    candidates = [
        f"{instance_root}/api/swagger/v2/odata",
        f"{instance_root}/api/swagger/v2/methods",
        f"{instance_root}/api/swagger/v2/odata/index.html",
        f"{instance_root}/api/swagger/v2/methods/index.html",
        f"{instance_root}/api/help/v2/index.html",
    ]
    service_list_endpoints = [
        f"{instance_root}/api/helppage/services?serviceType={svc_type}" for svc_type in service_types
    ]

    found: set[str] = set()
    discovery_log: list[dict] = []

    for endpoint in candidates + service_list_endpoints:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=timeout)
        except Exception:
            discovery_log.append({"endpoint": endpoint, "ok": False, "status": None})
            continue

        discovery_log.append({"endpoint": endpoint, "ok": resp.ok, "status": resp.status_code})
        if not resp.ok:
            continue

        text = resp.text or ""
        found.update(extract_swagger_links(text, endpoint, instance_root))

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                payload = resp.json()
                for text_val in iter_strings(payload):
                    t = text_val.strip()
                    if (
                        "/api/swagger/v2/odata/" in t or "/api/swagger/v2/methods/" in t
                    ) and t.endswith((".json", ".yaml")):
                        found.add(urljoin(instance_root + "/", t.lstrip("/")))

                service_names = extract_service_names(payload)
                for service_name in service_names:
                    found.add(f"{instance_root}/api/swagger/v2/odata/{service_name}.json")
                    found.add(f"{instance_root}/api/swagger/v2/odata/{service_name}.yaml")
                    found.add(f"{instance_root}/api/swagger/v2/methods/{service_name}.json")
                    found.add(f"{instance_root}/api/swagger/v2/methods/{service_name}.yaml")
            except Exception:
                pass

    return sorted(found), discovery_log


def filter_urls(urls: list[str], fmt: str) -> list[str]:
    if fmt == "json":
        return [u for u in urls if u.lower().endswith(".json")]
    if fmt == "yaml":
        return [u for u in urls if u.lower().endswith(".yaml")]
    return urls


def filter_by_scope(urls: list[str], surface: str, service_type: str | None, services: set[str]) -> list[str]:
    scoped = urls
    if surface != "both":
        scoped = [u for u in scoped if classify_surface(u) == surface]
    if service_type:
        scoped = [u for u in scoped if classify_service_type(u).lower() == service_type.lower()]
    if services:
        scoped = [u for u in scoped if classify_service_name(u) in services]
    return scoped


def save_spec(
    url: str,
    out_root: Path,
    timeout: int,
    headers: dict,
    overwrite: bool = False,
    missing_only: bool = False,
) -> tuple[str, str, dict | None]:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    safe_name = sanitize_name(name)
    surface_dir = classify_surface(url)
    target_dir = out_root / surface_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name

    if target.exists() and missing_only and not overwrite:
        try:
            version_info = extract_version_info_from_bytes(target.read_bytes(), url)
        except Exception:
            version_info = None
        return "skipped", str(target), version_info

    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        if not resp.ok:
            body = (resp.text or "")[:200]
            return "failed", f"{resp.status_code} {body}", None

        existing_bytes = target.read_bytes() if target.exists() else None
        content = resp.content

        if existing_bytes == content and not overwrite:
            version_info = extract_version_info_from_bytes(content, url)
            return "unchanged", str(target), version_info

        target.write_bytes(content)
        version_info = extract_version_info_from_bytes(content, url)
        return ("updated" if existing_bytes is not None else "downloaded"), str(target), version_info
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        return "failed", str(exc), None


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull Kinetic Swagger/OData specs into local API store")
    parser.add_argument("--env", default=None, help="Environment nickname")
    parser.add_argument("--user", default=None, help="User ID")
    parser.add_argument("--company", default=None, help="Company override for auth context")
    parser.add_argument("--out-dir", default="projects/Kinetic-API-Store", help="Output root directory")
    parser.add_argument("--format", choices=["json", "yaml", "both"], default="json", help="Spec format filter")
    parser.add_argument("--surface", choices=["odata", "methods", "both"], default="both", help="Swagger surface filter")
    parser.add_argument("--service-type", choices=["BO", "Proc", "Lib", "Rpt", "UD"], default=None, help="Service type filter")
    parser.add_argument("--service", action="append", default=[], help="Exact service name to pull; may be repeated")
    parser.add_argument("--env-subdir", action="store_true", help="Store files under an environment subdirectory")
    parser.add_argument("--missing-only", action="store_true", help="Skip files that already exist instead of checking for updates")
    parser.add_argument("--overwrite", action="store_true", help="Re-download files even when they already exist")
    parser.add_argument("--connection-version", default=None, help="Explicit connection version (e.g., 2025.2.14) for major-version gating")
    parser.add_argument("--force-scan", action="store_true", help="Bypass major-version gate and scan regardless")
    parser.add_argument("--timeout", type=int, default=60, help="HTTP timeout in seconds")
    parser.add_argument("--show-discovery", action="store_true", help="Print discovery endpoint statuses")
    args = parser.parse_args()

    env_name = args.env or os.environ.get("KIN_ENV_NAME")
    user_id = args.user or os.environ.get("KIN_USER") or os.environ.get("KIN_USER_ID")
    company_id = args.company or os.environ.get("KIN_COMPANY")

    mgr, config = resolve_active_config(env_name, user_id, company_id)
    headers = build_headers(mgr, config)

    env_dir = sanitize_name(config.get("nickname") or "default")
    out_root = Path(args.out_dir)
    if args.env_subdir:
        out_root = out_root / env_dir
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.json"
    history_path = out_root / "connection-history.yaml"
    history_entries = load_version_history_yaml(history_path)

    instance_root = derive_instance_root(str(config["url"]))
    connection_fingerprint = fingerprint_text(instance_root)
    server_version = probe_server_version(instance_root, headers, args.timeout)
    connection_version = (args.connection_version or str(server_version.get("value", ""))).strip()
    major_version = normalize_major_version(connection_version)

    if major_version and not args.force_scan and major_version_already_pulled(history_entries, connection_fingerprint, major_version):
        print(
            f"⏭ Skipping pull: major connection version {major_version} already exists in connection-history.yaml for this connection fingerprint."
        )
        print("   Use --force-scan to pull anyway.")
        sys.exit(0)

    print(f"🔎 Discovering API specs from: {instance_root}")

    urls, discovery_log = discover_spec_urls(instance_root, headers, args.timeout)
    urls = filter_urls(urls, args.format)
    service_filters = {s.strip() for s in args.service if s and s.strip()}
    urls = filter_by_scope(urls, args.surface, args.service_type, service_filters)

    if args.show_discovery:
        for d in discovery_log:
            status = d.get("status") if d.get("status") is not None else "ERR"
            print(f"  - [{status}] {d['endpoint']}")

    if not urls:
        print("❌ No swagger spec URLs discovered.")
        sys.exit(2)

    print(f"📥 Downloading {len(urls)} spec file(s) to: {out_root}")

    ok_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_count = 0
    failed: list[dict] = []
    saved_files: list[str] = []
    versions: list[dict] = []
    interrupted = False
    interrupted_at = None

    try:
        for idx, url in enumerate(urls, start=1):
            status, info, version_info = save_spec(
                url,
                out_root,
                args.timeout,
                headers,
                overwrite=args.overwrite,
                missing_only=args.missing_only,
            )
            if status == "downloaded":
                ok_count += 1
                saved_files.append(info)
                if version_info:
                    versions.append(version_info)
                print(f"  [{idx}/{len(urls)}] ✅ {os.path.basename(info)}")
            elif status == "updated":
                updated_count += 1
                saved_files.append(info)
                if version_info:
                    versions.append(version_info)
                print(f"  [{idx}/{len(urls)}] ♻ {os.path.basename(info)} (updated)")
            elif status == "unchanged":
                unchanged_count += 1
                saved_files.append(info)
                if version_info:
                    versions.append(version_info)
                print(f"  [{idx}/{len(urls)}] = {os.path.basename(info)} (unchanged)")
            elif status == "skipped":
                skipped_count += 1
                saved_files.append(info)
                if version_info:
                    versions.append(version_info)
                print(f"  [{idx}/{len(urls)}] ↷ {os.path.basename(info)} (exists)")
            else:
                failed.append(
                    {
                        "surface": classify_surface(url),
                        "service_type": classify_service_type(url),
                        "error": redact_free_text(info),
                    }
                )
                print(f"  [{idx}/{len(urls)}] ❌ {url} :: {info}")
    except KeyboardInterrupt:
        interrupted = True
        interrupted_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print("\n⚠️ Pull interrupted by user. Writing partial manifest...")

    version_summary = sorted(
        {
            (item.get("surface", ""), item.get("service_type", ""), item.get("service_name", ""), item.get("version", ""), item.get("openapi", ""))
            for item in versions
        }
    )

    pulls_dir = out_root / "_pulls"
    pulls_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    manifest = {
        "pulled_at_utc": timestamp,
        "connection_fingerprint": connection_fingerprint,
        "server_version": {
            "kind": server_version.get("kind", ""),
            "name": server_version.get("name", ""),
            "value": connection_version,
            "major": major_version,
        },
        "connection_version": connection_version,
        "major_version": major_version,
        "requested_format": args.format,
        "requested_surface": args.surface,
        "requested_service_type": args.service_type,
        "requested_service_count": len(service_filters),
        "store_root": str(out_root),
        "env_subdir": bool(args.env_subdir),
        "request_headers_redacted": redact_headers(headers),
        "discovery": summarize_discovery(discovery_log),
        "discovered_count": len(urls),
        "downloaded_count": ok_count,
        "updated_count": updated_count,
        "unchanged_count": unchanged_count,
        "skipped_count": skipped_count,
        "failed_count": len(failed),
        "interrupted": interrupted,
        "interrupted_at_utc": interrupted_at,
        "files_written_count": len(saved_files),
        "version_summary": [
            {
                "surface": surface,
                "service_type": service_type,
                "version": version,
                "openapi": openapi,
            }
            for surface, service_type, service_name, version, openapi in version_summary
        ],
        "failed": failed,
    }

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    pull_manifest_path = pulls_dir / f"pull_{timestamp}.json"
    pull_manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    history_entries.append(
        {
            "pulled_at_utc": timestamp,
            "connection_fingerprint": connection_fingerprint,
            "connection_version": connection_version,
            "major_version": major_version,
            "requested_surface": args.surface,
            "requested_format": args.format,
            "downloaded_count": ok_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
        }
    )
    write_version_history_yaml(history_path, history_entries)

    if interrupted:
        print(
            f"⚠️ Partial pull saved: {ok_count} downloaded, {updated_count} updated, "
            f"{unchanged_count} unchanged, {skipped_count} skipped, {len(failed)} failed."
        )
        print(f"🧾 Manifest: {manifest_path}")
        sys.exit(130)

    if failed:
        print(
            f"⚠️ Completed with failures: {ok_count} downloaded, {updated_count} updated, "
            f"{unchanged_count} unchanged, {skipped_count} skipped, {len(failed)} failed."
        )
        print(f"🧾 Manifest: {manifest_path}")
        sys.exit(3)

    print(
        f"✅ Completed: {ok_count} downloaded, {updated_count} updated, "
        f"{unchanged_count} unchanged, {skipped_count} skipped, {len(failed)} failed."
    )
    print(f"🧾 Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
