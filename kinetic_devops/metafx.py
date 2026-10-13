"""
kinetic_devops/metafx.py

Metafetcher - Query Epicor Kinetic metadata and system information.

Provides methods to:
- Fetch metadata about Business Objects and services
- Query system configuration
- Retrieve customization information
- Inspect BO structures and fields
"""

from __future__ import annotations

# kinetic_devops/metafx.py
import os
import sys
import json
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
import argparse
import re
import datetime as dt
from typing import Optional, Dict, Any, Iterable, List
from urllib.parse import quote, urlparse
 
if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient

# File: kinetic_devops/metafx.py

LAYER_UPLOAD_MARKER = "/ImportLayers"
LAYER_DELETE_MARKER = "/BulkDeleteLayers"

OPS_TO_INTERNAL = {
    "import": "upload",
    "delete": "delete",
    "both": "both",
}

class KineticMetafetcher(KineticBaseClient):
    def __init__(self, env_nickname: Optional[str] = None, user_id: Optional[str] = None, company_id: Optional[str] = None):
        super().__init__(env_nickname=env_nickname, user_id=user_id, company_id=company_id, debug=False)

    def _service_url(self, method_name: str, company: str = "") -> str:
        target_co = company or self.config["company"]
        return f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/Ice.Lib.MetaFXSvc/{method_name}"

    def call_service(self, method_name: str, payload: Optional[Dict[str, Any]] = None, company: str = "") -> Dict[str, Any]:
        target_co = company or self.config["company"]
        return self.execute_request("POST", self._service_url(method_name, company=target_co), payload=payload or {}, company=target_co)

    def get_layers(
        self,
        view_id: str,
        type_code: str,
        device_type: str = "Desktop",
        include_unpublished_layers: bool = True,
        company: str = "",
    ) -> List[Dict[str, Any]]:
        response = self.call_service(
            "GetLayers",
            {
                "request": {
                    "ViewId": view_id,
                    "TypeCode": type_code,
                    "DeviceType": device_type,
                    "IncludeUnpublishedLayers": bool(include_unpublished_layers),
                }
            },
            company=company,
        )
        rows = response.get("returnObj") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []

    def bulk_delete_layers(self, layers_to_delete: List[Dict[str, Any]], company: str = "") -> Dict[str, Any]:
        payload_rows: List[Dict[str, Any]] = []
        for layer in layers_to_delete:
            payload_rows.append(
                {
                    "Id": layer.get("Id"),
                    "SubType": layer.get("SubType"),
                    "LastUpdated": layer.get("LastUpdated"),
                    "IsPublished": bool(layer.get("IsPublished", False)),
                    "IsSilentExport": bool(layer.get("IsSilentExport", False)),
                    "ViewId": layer.get("ViewId"),
                    "TypeCode": layer.get("TypeCode"),
                    "Company": layer.get("Company"),
                    "LayerName": layer.get("LayerName"),
                    "DeviceType": layer.get("DeviceType") or "Desktop",
                    "CGCCode": layer.get("CGCCode") or "",
                    "SystemFlag": bool(layer.get("SystemFlag", False)),
                    "HasDraftContent": bool(layer.get("HasDraftContent", False)),
                    "LastUpdatedBy": layer.get("LastUpdatedBy"),
                }
            )
        return self.call_service("BulkDeleteLayers", {"layersToDelete": payload_rows}, company=company)

    def delete_layer(self, layer_row: Dict[str, Any], company: str = "") -> Dict[str, Any]:
        request = {
            "ViewId": layer_row.get("ViewId"),
            "Company": layer_row.get("Company"),
            "TypeCode": layer_row.get("TypeCode"),
            "LayerName": layer_row.get("LayerName"),
            "DeviceType": layer_row.get("DeviceType") or "Desktop",
            "CGCCode": layer_row.get("CGCCode") or "",
            "UserName": self.config.get("user_id") or "",
            "IncludeDraftContent": bool(layer_row.get("HasDraftContent", True)),
            "UxAppVersion": 0,
        }
        return self.call_service("DeleteLayer", {"request": request}, company=company)

    def fetch_ui_metadata(self, app_id: str, menu_id: str):
        """Fetches UI Metadata via Ice.LIB.MetaFXSvc/GetApp."""
        base_url = self.config['url'].rstrip('/')
        
        # Construct the specialized MetaFX request object
        request_obj = {
            "id": app_id,
            "properties": {
                "deviceType": "Desktop",
                "layers": [],
                "applicationType": "view",
                "additionalContext": {
                    "menuId": menu_id
                }
            }
        }
        
        url = f"{base_url}/api/v2/odata/{self.config['company']}/Ice.LIB.MetaFXSvc/GetApp"
        params = {"request": json.dumps(request_obj)}
        
        # MetaFX often requires this specific serialization header found in UX traces
        extra_headers = {
            "x-epi-extension-serialization": "full-metadata"
        }

        print(f"\n--- Requesting MetaFX UI Layout ---")
        print(f"App ID: {app_id}")
        
        try:
            # Leverage base client for logging, session touching, and auth
            data = self.execute_request("GET", url, params=params, extra_headers=extra_headers)

            filename = f"ui_{app_id}_{menu_id}.json"
            filename = self.resolve_output_path(filename, conflict_resolution="timestamp")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
                
            print(f"✅ UI Metadata saved to: {filename}")

        except Exception as e:
            print(f"Connection Error: {e}")

    def _replace_placeholders(self, text: str, mapping: Dict[str, str]) -> str:
        def repl(match: "re.Match[str]") -> str:
            key = match.group(1)
            return mapping.get(key, match.group(0))

        return re.sub(r"\{([A-Za-z0-9_]+)\}", repl, text)

    def _deep_replace_placeholders(self, value: Any, mapping: Dict[str, str]) -> Any:
        if isinstance(value, str):
            return self._replace_placeholders(value, mapping)
        if isinstance(value, dict):
            return {k: self._deep_replace_placeholders(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [self._deep_replace_placeholders(v, mapping) for v in value]
        return value

    def _iter_bocalls(self, node: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(node, dict):
            bo = node.get("BOCall")
            if isinstance(bo, dict):
                yield bo
            for value in node.values():
                yield from self._iter_bocalls(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_bocalls(item)

    def _classify_operation(self, url: str) -> Optional[str]:
        if LAYER_UPLOAD_MARKER in url:
            return "upload"
        if LAYER_DELETE_MARKER in url:
            return "delete"
        return None

    def _normalize_call_url(self, raw_url: str, mapping: Dict[str, str]) -> str:
        raw_url = self._replace_placeholders(raw_url.strip(), mapping)
        base_url = self.config["url"].rstrip("/")
        company = self.config["company"]

        m = re.search(r"/api/v2/odata/[^/]+/(.+)$", raw_url)
        if m:
            return f"{base_url}/api/v2/odata/{company}/{m.group(1)}"
        return raw_url

    def _parse_callinfo_correlation(self, headers: Dict[str, Any]) -> str:
        callinfo = headers.get("callinfo")
        if isinstance(callinfo, dict):
            return str(callinfo.get("CorrelationId", ""))
        if isinstance(callinfo, str):
            try:
                parsed = json.loads(callinfo)
                if isinstance(parsed, dict):
                    return str(parsed.get("CorrelationId", ""))
            except Exception:
                return ""
        return ""

    def _short_response_body(self, resp: requests.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            text = (resp.text or "").strip()
            return text[:4000] + "...<truncated>" if len(text) > 4000 else text


    def _load_jsonc_file(self, path: str) -> Any:
        """Load JSON or JSONC payloads from exported dump files.

        Supports line/block comments and trailing commas to match real-world
        Postman/export artifacts that are JSON-like but not strict JSON.
        """
        with open(path, "r", encoding="utf-8") as f:
            raw_text = f.read()

        stripped_chars: List[str] = []
        index = 0
        in_string = False
        escape = False
        in_line_comment = False
        in_block_comment = False

        while index < len(raw_text):
            char = raw_text[index]
            next_char = raw_text[index + 1] if index + 1 < len(raw_text) else ""

            if in_line_comment:
                if char in "\r\n":
                    in_line_comment = False
                    stripped_chars.append(char)
                index += 1
                continue

            if in_block_comment:
                if char == "*" and next_char == "/":
                    in_block_comment = False
                    index += 2
                else:
                    index += 1
                continue

            if in_string:
                stripped_chars.append(char)
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                index += 1
                continue

            if char == '"':
                in_string = True
                stripped_chars.append(char)
                index += 1
                continue

            if char == "/" and next_char == "/":
                in_line_comment = True
                index += 2
                continue

            if char == "/" and next_char == "*":
                in_block_comment = True
                index += 2
                continue

            stripped_chars.append(char)
            index += 1

        cleaned_text = "".join(stripped_chars)
        cleaned_text = re.sub(r",(?=\s*[}\]])", "", cleaned_text)
        return json.loads(cleaned_text)

    def _collect_layer_calls(self, files: List[str], only: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []
        for path in files:
            payload = self._load_jsonc_file(path)
            for bo in self._iter_bocalls(payload):
                request = bo.get("request", {})
                if not isinstance(request, dict):
                    continue
                op = self._classify_operation(str(request.get("url", "")))
                if not op:
                    continue
                if only != "both" and op != only:
                    continue
                calls.append(
                    {
                        "source_file": path,
                        "id": bo.get("id", ""),
                        "operation": op,
                        "request": request,
                    }
                )
        return calls

    def _order_calls_for_ops(self, calls: List[Dict[str, Any]], ops: str) -> List[Dict[str, Any]]:
        """
        Deployment semantics:
        - import: delete first, then import
        - delete: delete only
        - both: delete first, then import
        """
        deletes = [c for c in calls if c.get("operation") == "delete"]
        uploads = [c for c in calls if c.get("operation") == "upload"]

        if ops == "delete":
            return deletes
        if ops == "import":
            return deletes + uploads
        return deletes + uploads

    def run_layer_operations(
        self,
        files: List[str],
        ops: str = "import",
        plant: str = "",
        timeout: int = 120,
        dry_run: bool = False,
        report_path: str = "",
    ) -> int:
        for path in files:
            if not os.path.exists(path):
                print(f"❌ Dump file not found: {path}")
                return 2

        internal_ops = OPS_TO_INTERNAL.get(ops, "upload")

        mapping = self._build_runtime_substitutions(plant=plant)
        candidates = self._collect_layer_calls(files, "both" if internal_ops == "upload" else internal_ops)
        candidates = self._order_calls_for_ops(candidates, ops)
        if not candidates:
            print("❌ No matching layer operations found in provided dump files.")
            return 3

        if ops == "import" and not any(c.get("operation") == "upload" for c in candidates):
            print("❌ No ImportLayers operation found. Cannot perform import deployment.")
            return 3

        if ops == "import" and not any(c.get("operation") == "delete" for c in candidates):
            print("⚠️  No delete operation found; proceeding with import only.")

        print(f"Found {len(candidates)} layer operation(s) to process.")

        results: List[Dict[str, Any]] = []
        success_count = 0
        fail_count = 0

        for idx, call in enumerate(candidates, start=1):
            request = call["request"]
            raw_url = str(request.get("url", ""))
            method = str(request.get("method", "POST")).upper()
            final_url = self._normalize_call_url(raw_url, mapping)
            body = self._deep_replace_placeholders(request.get("body", {}), mapping)

            headers = self.mgr.get_auth_headers(self.config)
            headers.update(
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json, text/plain, */*",
                    "x-epi-request-etag": "true",
                    "x-epi-extension-serialization": "full-metadata",
                }
            )
            if plant:
                headers["callSettings"] = json.dumps({"Company": self.config["company"], "Plant": plant})

            item = {
                "index": idx,
                "operation": call["operation"],
                "source_file": call["source_file"],
                "call_id": call.get("id", ""),
                "method": method,
                "url": final_url,
                "dry_run": dry_run,
            }

            if dry_run:
                print(f"[DRY-RUN] {idx}/{len(candidates)} {call['operation'].upper()} {final_url}")
                item.update({"success": True, "status_code": None, "correlation_id": "", "response": "dry-run"})
                success_count += 1
                results.append(item)
                continue

            print(f"[{idx}/{len(candidates)}] {call['operation'].upper()} {final_url}")

            try:
                # Execute request directly to maintain access to status/headers for the report
                resp = requests.request(
                    method=method,
                    url=final_url,
                    headers=headers,
                    json=body,
                    timeout=timeout
                )

                # Standardized wire logging for observability and session management
                self.log_wire(method, final_url, headers, body=body, resp=resp)
                if resp.ok:
                    self.mgr.touch_from_headers(resp.request.headers)

                ok = 200 <= resp.status_code < 300
                item.update(
                    {
                        "success": ok,
                        "status_code": resp.status_code,
                        "correlation_id": self._parse_callinfo_correlation(resp.headers),
                        "response": self._short_response_body(resp),
                    }
                )
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as exc:
                fail_count += 1
                item.update(
                    {
                        "success": False,
                        "status_code": None,
                        "correlation_id": "",
                        "response": str(exc),
                    }
                )

            results.append(item)

        if not report_path:
            ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            report_path = os.path.join("temp", f"layer_ops_report_{ts}.json")

        report_dir = os.path.dirname(report_path)
        if report_dir:
            os.makedirs(report_dir, exist_ok=True)

        report = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "environment": self.config.get("nickname", ""),
            "user_id": self.config.get("user_id", ""),
            "company": self.config.get("company", ""),
            "input_files": files,
            "summary": {
                "total": len(results),
                "success": success_count,
                "failed": fail_count,
                "dry_run": dry_run,
                "ops": ops,
            },
            "results": results,
        }

        report_path = self.resolve_output_path(report_path, conflict_resolution="timestamp")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"\nReport written: {report_path}")
        if fail_count > 0:
            print("❌ Some operations failed. See report for details.")
            return 1

        print("✅ All operations completed successfully.")
        return 0

def main():
    parser = argparse.ArgumentParser(
        description=(
            "MetaFX tools for UI metadata and core layer operations (Ice.Lib.MetaFXSvc).\n"
            "Global options (-e/-u/--company and the file options) go BEFORE the subcommand.\n"
            "With no subcommand, runs 'fetch' and prompts for the App ID and Menu ID."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.metafx -e MyTestEnv fetch -a Erp.UI.CustShipEntry -m MyMenuId\n"
            "  python -m kinetic_devops.metafx -e MyTestEnv layers layer_dump.json --dry-run"
        ),
    )
    parser.add_argument(
        "-e", "--env", metavar="ENV",
        help="Environment nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument("-u", "--user", metavar="USER_ID", help="Epicor user ID of the session to use for ENV.")
    parser.add_argument("--company", metavar="COMPANY", help="Company ID to work in (default: the session's company).")
    KineticBaseClient.add_file_resolution_args(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch UI metadata via GetApp",
        description=(
            "Read-only: calls Ice.Lib.MetaFXSvc/GetApp for an app/menu and saves the UI layout JSON to "
            "ui_<APP>_<MENU>.json in the current directory (timestamped copy if it already exists). "
            "Missing -a/-m values are prompted for."
        ),
    )
    fetch_parser.add_argument("-a", "--app", metavar="APP_ID", help="App ID, e.g. Erp.UI.CustShipEntry (prompted if omitted).")
    fetch_parser.add_argument("-m", "--menu", metavar="MENU_ID", help="Menu ID the app is launched from (prompted if omitted).")

    layers_parser = subparsers.add_parser(
        "layers",
        help="Core layer operations (default deploy = delete then import)",
        description=(
            "WRITES TO EPICOR: replays the MetaFX BulkDeleteLayers / ImportLayers calls (BOCall entries)\n"
            "found in exported dump files against the selected environment. URLs are re-pointed at the\n"
            "session's server/company and {HOSTNAME}/{COMPANY}/{USER_ID}/{PLANT}-style placeholders are\n"
            "filled in. Ops:\n"
            "  import  run the dump's delete calls, then its import calls (default; a deploy)\n"
            "  delete  DESTRUCTIVE: run only the delete calls\n"
            "  both    same order as import (delete, then import)\n"
            "A JSON report of every call is written; exit code is non-zero if any call fails."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.metafx -e MyTestEnv layers layer_dump.json --dry-run\n"
            "  python -m kinetic_devops.metafx -e MyTestEnv layers layer_dump.json --report temp/deploy.json"
        ),
    )
    layers_parser.add_argument(
        "files", nargs="+", metavar="DUMP_FILE",
        help="Dump JSON files (JSON or JSONC export containing BOCall request entries)",
    )
    layers_parser.add_argument(
        "--ops", choices=["import", "delete", "both"], default="import",
        help="Which layer calls to run: import, delete or both (see above; default: import).",
    )
    layers_parser.add_argument(
        "--plant", metavar="PLANT", default="",
        help="Plant ID: fills {PLANT} placeholders and is sent in the callSettings header (default: none).",
    )
    layers_parser.add_argument(
        "--timeout", metavar="SECONDS", type=int, default=120,
        help="HTTP timeout per call, in seconds (default: 120).",
    )
    layers_parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the calls that would be made and write the report without sending them "
             "(a session is still resolved).",
    )
    layers_parser.add_argument(
        "--report", metavar="PATH", default="",
        help="Report file path (default: temp/layer_ops_report_<timestamp>.json).",
    )


    args = parser.parse_args()

    # Backward-compatible behavior: if no subcommand, run fetch flow.
    command = args.command or "fetch"

    fetcher = KineticMetafetcher(env_nickname=args.env, user_id=args.user, company_id=args.company)
    fetcher.configure_file_resolution_from_args(args)

    if command == "layers":
        rc = fetcher.run_layer_operations(
            files=args.files,
            ops=args.ops,
            plant=args.plant,
            timeout=args.timeout,
            dry_run=args.dry_run,
            report_path=args.report,
        )
        sys.exit(rc)

    app_id = getattr(args, "app", None) or input("Enter App ID (e.g. Erp.UI.CustShipEntry): ").strip()
    menu_id = getattr(args, "menu", None) or input("Enter Menu ID: ").strip()

    fetcher.fetch_ui_metadata(app_id, menu_id)

if __name__ == "__main__":
    main()