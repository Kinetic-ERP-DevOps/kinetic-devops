"""Programmatic UD column drift detection and sync via Ice.BO.ZDataTableSvc."""

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient


SKIP_SYNC_FIELDS = {
    "SysRevID",
    "SysRowID",
    "BitFlag",
    "RowMod",
    "Company",
}

COMPARE_FIELDS = [
    "DataType",
    "Required",
    "ReadOnly",
    "Description",
    "FieldFormat",
    "FieldScale",
    "LikeDataFieldSystemCode",
    "LikeDataFieldTableID",
    "LikeDataFieldName",
    "InitialValue",
    "DefaultFormat",
    "DefaultLabel",
    "DBTableName",
]


def _zdt_url(base_url: str, company: str, method: str) -> str:
    return f"{base_url.rstrip('/')}/api/v2/odata/{company}/Ice.BO.ZDataTableSvc/{method}"


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _coerce_text(text: Optional[str]) -> Any:
    if text is None:
        return ""
    value = text.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return value


def parse_zdatatable_xml(xml_path: str) -> Dict[str, Any]:
    tree = ET.parse(xml_path)
    root = tree.getroot()

    table: Dict[str, Any] = {}
    fields: List[Dict[str, Any]] = []

    for child in root:
        name = _strip_ns(child.tag)
        if name == "ZDataTable":
            for node in child:
                table[_strip_ns(node.tag)] = _coerce_text(node.text)
        elif name == "ZDataField":
            row: Dict[str, Any] = {}
            for node in child:
                row[_strip_ns(node.tag)] = _coerce_text(node.text)
            if row.get("FieldName"):
                fields.append(row)

    if not table:
        raise ValueError("XML missing ZDataTable node")
    if not table.get("SystemCode") or not table.get("DataTableID"):
        raise ValueError("XML missing required ZDataTable keys: SystemCode/DataTableID")

    return {
        "table": table,
        "fields": fields,
    }


def _normalize_for_compare(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def diff_field_rows(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    diffs: Dict[str, Dict[str, Any]] = {}
    for key in COMPARE_FIELDS:
        src = _normalize_for_compare(source.get(key, ""))
        tgt = _normalize_for_compare(target.get(key, ""))
        if src != tgt:
            diffs[key] = {"source": source.get(key), "target": target.get(key)}
    return diffs


class KineticZDataTableService(KineticBaseClient):
    def _call(self, method: str, payload: Optional[Dict[str, Any]] = None, company: str = "") -> Dict[str, Any]:
        target_co = company or self.config["company"]
        return self.execute_request("POST", _zdt_url(self.config["url"], target_co, method), payload=payload or {}, company=target_co)

    def get_by_id_ud(self, system_code: str, table_id: str, company: str = "") -> Dict[str, Any]:
        response = self._call(
            "GetByIDUd",
            {"systemCode": system_code, "dataTableID": table_id},
            company=company,
        )
        return response.get("returnObj") or {}

    def exists_ud_table(self, system_code: str, table_id: str, company: str = "") -> bool:
        try:
            ds = self.get_by_id_ud(system_code, table_id, company=company)
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (400, 404):
                return False
            raise
        rows = ds.get("ZDataTable") or []
        return bool(rows)

    def get_extended_sync_details(self, schema_name: str, table_name: str, company: str = "") -> Dict[str, Any]:
        response = self._call(
            "GetExtendedTableSyncDetailsMessage",
            {"schemaName": schema_name, "tableName": table_name},
            company=company,
        )
        return {
            "in_sync": response.get("returnObj"),
            "details": response.get("parameters") or {},
        }

    def get_new_zdata_field(self, ds: Dict[str, Any], system_code: str, table_id: str, company: str = "") -> Dict[str, Any]:
        response = self._call(
            "GetNewZDataField",
            {"ds": ds, "systemCode": system_code, "dataTableID": table_id},
            company=company,
        )
        return (response.get("parameters") or {}).get("ds") or ds

    def update(self, ds: Dict[str, Any], company: str = "") -> Dict[str, Any]:
        response = self._call("Update", {"ds": ds}, company=company)
        return (response.get("parameters") or {}).get("ds") or ds

    def sync_fields_from_xml(
        self,
        xml_path: str,
        company: str = "",
        apply_changes: bool = False,
        update_conflicts: bool = False,
        system_code_override: str = "",
        table_id_override: str = "",
    ) -> Dict[str, Any]:
        source = parse_zdatatable_xml(xml_path)
        source_table = source["table"]
        source_fields = source["fields"]

        system_code = system_code_override.strip() or str(source_table.get("SystemCode", "")).strip()
        table_id = table_id_override.strip() or str(source_table.get("DataTableID", "")).strip()

        target_ds = self.get_by_id_ud(system_code, table_id, company=company)
        target_fields = target_ds.get("ZDataField") or []
        target_lookup = {str(row.get("FieldName", "")).strip(): row for row in target_fields}

        missing: List[Dict[str, Any]] = []
        drift: List[Dict[str, Any]] = []
        same: List[str] = []

        for src in source_fields:
            field_name = str(src.get("FieldName", "")).strip()
            if not field_name:
                continue
            tgt = target_lookup.get(field_name)
            if not tgt:
                missing.append(src)
                continue
            diffs = diff_field_rows(src, tgt)
            if diffs:
                drift.append({"field": field_name, "diffs": diffs})
            else:
                same.append(field_name)

        created_fields: List[str] = []
        updated_fields: List[str] = []
        update_attempted = False

        if apply_changes and (missing or (update_conflicts and drift)):
            working_ds = target_ds

            for src in missing:
                field_name = str(src.get("FieldName", "")).strip()
                before_len = len((working_ds.get("ZDataField") or []))
                working_ds = self.get_new_zdata_field(working_ds, system_code, table_id, company=company)
                after_rows = working_ds.get("ZDataField") or []
                if len(after_rows) <= before_len:
                    continue
                row = after_rows[-1]
                for key, value in src.items():
                    if key in SKIP_SYNC_FIELDS:
                        continue
                    if key in row:
                        row[key] = value
                row["SystemCode"] = system_code
                row["DataTableID"] = table_id
                row["RowMod"] = "A"
                created_fields.append(field_name)

            if update_conflicts:
                for item in drift:
                    field_name = item["field"]
                    src = next((r for r in source_fields if str(r.get("FieldName", "")).strip() == field_name), None)
                    tgt = next((r for r in (working_ds.get("ZDataField") or []) if str(r.get("FieldName", "")).strip() == field_name), None)
                    if not src or not tgt:
                        continue
                    for key in COMPARE_FIELDS:
                        if key in src and key in tgt and key not in SKIP_SYNC_FIELDS:
                            tgt[key] = src.get(key)
                    tgt["RowMod"] = "U"
                    updated_fields.append(field_name)

            working_ds = self.update(working_ds, company=company)
            update_attempted = True
            target_ds = working_ds

        sync_details = self.get_extended_sync_details(
            schema_name=str(source_table.get("SchemaName", "")).strip() or system_code,
            table_name=str(source_table.get("DBTableName", "")).strip() or table_id,
            company=company,
        )

        return {
            "xml_path": xml_path,
            "system_code": system_code,
            "data_table_id": table_id,
            "source_field_count": len(source_fields),
            "target_field_count": len(target_fields),
            "same_fields": same,
            "missing_fields": [str(row.get("FieldName", "")).strip() for row in missing],
            "drift_fields": drift,
            "apply_changes": apply_changes,
            "update_conflicts": update_conflicts,
            "update_attempted": update_attempted,
            "created_fields": created_fields,
            "updated_fields": updated_fields,
            "extended_sync": sync_details,
            "post_update_field_count": len((target_ds.get("ZDataField") or [])),
        }


def _default_report_path(table_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_table = table_id.replace("/", "_").replace("\\", "_")
    return os.path.join("temp", f"zdatatable_sync_{safe_table}_{ts}.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Drift detect/sync UD fields from ZDataTable XML.\n"
            "Compares the UD fields in a ZDataTable XML extract with the target environment's UD table\n"
            "(Ice.BO.ZDataTableSvc) and reports missing, drifted and identical fields. Read-only by default;\n"
            "--apply WRITES TO EPICOR (creates missing fields, and with --update-conflicts overwrites drifted\n"
            "field definitions). The JSON report is printed and saved to a file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.zdatatable NonConf_UD.xml --env MyTestEnv              # report only\n"
            "  python -m kinetic_devops.zdatatable NonConf_UD.xml --env MyTestEnv --apply      # create missing"
        ),
    )
    parser.add_argument("xml_file", metavar="XML_FILE", help="Path to ZDataTable XML extract")
    parser.add_argument(
        "--env", metavar="ENV",
        help="Environment nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument("--user", metavar="USER_ID", help="User ID of the session to use for ENV")
    parser.add_argument("--company", metavar="COMPANY", default="", help="Company override (default: the session's company)")
    parser.add_argument(
        "--system-code", metavar="CODE", default="",
        help="Override SystemCode (e.g., Erp). Default: taken from the XML.",
    )
    parser.add_argument(
        "--table-id", metavar="TABLE", default="",
        help="Override DataTableID (e.g., NonConf_UD). Default: taken from the XML.",
    )
    parser.add_argument("--apply", action="store_true", help="WRITES TO EPICOR: Create missing fields in the target table")
    parser.add_argument(
        "--update-conflicts", action="store_true",
        help="With --apply: also Update fields with drift to match source (no effect without --apply)",
    )
    parser.add_argument(
        "--report", metavar="PATH", default="",
        help="Optional output report path (default: temp/zdatatable_sync_<TABLE>_<UTC timestamp>.json)",
    )
    KineticBaseClient.add_file_resolution_args(parser)

    args = parser.parse_args()

    service = KineticZDataTableService(args.env, args.user, company_id=args.company or None)
    service.configure_file_resolution_from_args(args)
    report = service.sync_fields_from_xml(
        args.xml_file,
        company=args.company,
        apply_changes=args.apply,
        update_conflicts=args.update_conflicts,
        system_code_override=args.system_code,
        table_id_override=args.table_id,
    )

    report_path = args.report
    if not report_path:
        report_path = _default_report_path(report.get("data_table_id", "zdatatable"))

    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    report_path = service.resolve_output_path(report_path, conflict_resolution="timestamp")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nReport saved to {report_path}")


if __name__ == "__main__":
    main()
