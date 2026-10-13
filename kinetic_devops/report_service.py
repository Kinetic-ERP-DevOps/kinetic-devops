# kinetic_devops/report_service.py
"""
Report Service Wrapper

Handles uploading and extracting Report Data Definitions (RDD) and RDLs.
Inherits session management from KineticBaseClient.
"""

import sys
import json
import argparse
import base64
import os
from kinetic_devops import _lazy_import

requests = _lazy_import("requests")
from typing import Dict, Any, Optional

if __package__:
    from .base_client import KineticBaseClient
else:
    from kinetic_devops.base_client import KineticBaseClient

def _find_base64_field(obj: Any, path: str = "") -> Optional[Dict[str, Any]]:
    """Depth-first search for a long string value (report PDF bytes) inside
    an arbitrarily-nested response. GetReportBytes's exact response shape
    isn't fixed/documented -- ported from a verified TypeScript reference
    (tap_prod/base44/functions/epicorMes/handlers/reports.ts) that uses the
    same defensive search for the same reason."""
    if isinstance(obj, str) and len(obj) > 100:
        return {"path": path, "value": obj}
    if isinstance(obj, dict):
        for key, value in obj.items():
            result = _find_base64_field(value, f"{path}.{key}" if path else key)
            if result:
                return result
    return None


class KineticReportService(KineticBaseClient):
    """
    Service for managing Epicor Kinetic Reports.
    """

    def _rpt_call(self, service_name: str, method_name: str, payload: Dict[str, Any], company: str = "") -> Dict[str, Any]:
        target_co = company or self.config["company"]
        url = f"{self.config['url'].rstrip('/')}/api/v2/odata/{target_co}/{service_name}/{method_name}"
        return self.execute_request("POST", url, payload=payload, company=target_co)

    def submit_report_job(
        self,
        service_name: str,
        change_method: str,
        param_table: str,
        base_param: Dict[str, Any],
        agent_id: str = "SystemTaskAgent",
        maint_program: str = "",
        company: str = "",
    ) -> Dict[str, Any]:
        """Submit a report render job via Epicor's standard report-submission
        pattern: Change<Key> (a two-row dataset -- an unchanged baseline row
        plus a RowMod="U" row carrying the real parameters) followed by
        SubmitToAgent. Verified live-reference pattern (not guessed): ported
        from tap_prod/base44/functions/epicorMes/handlers/reports.ts's
        printJobTraveler, a "PDCA-validated"/"production-validated" call
        against Erp.Rpt.JobTravSvc. Each report's own BO (service_name),
        Change method, param table name, and the full base_param schema are
        report-specific -- this function is generic over all of those, but
        the caller must supply values verified for the target report (e.g.
        via pull_api_store.py against the report's own Erp.Rpt.*Svc), not
        assume JobTravParam's field list applies elsewhere.
        """
        change_payload = {
            "ds": {
                "ReportStyle": [],
                param_table: [
                    {**base_param, "RowMod": ""},
                    {**base_param, "RowMod": "U"},
                ],
            }
        }
        change_resp = self._rpt_call(service_name, change_method, change_payload, company=company)
        result_ds = change_resp.get("parameters", {}).get("ds") or change_resp.get("parameters") or change_resp

        # The reference implementation filters to rows where the report's own
        # key field is truthy (e.g. `p => p.Jobs` for JobTrav) to drop the
        # blank baseline row before SubmitToAgent. This function is generic
        # over the key field name, so it can't replicate that filter exactly
        # -- verified live that submitting both rows unchanged (baseline +
        # the real one) works fine regardless, so no filtering is applied.
        param_rows = result_ds.get(param_table) or []
        submit_payload = {
            "ds": {**result_ds, param_table: param_rows},
            "agentID": agent_id,
            "agentSchedNum": 0,
            "agentTaskNum": 0,
            "recurringTask": False,
            "maintProgram": maint_program,
        }
        return self._rpt_call(service_name, "SubmitToAgent", submit_payload, company=company)

    def submit_packing_slip_job(
        self,
        pack_num: int,
        workstation_id: str,
        style_num: int = 2,
        agent_id: str = "SystemTaskAgent",
        maint_program: str = "Erp.UI.Rpt.PackingSlipPrintTransaction",
        company: str = "",
    ) -> Dict[str, Any]:
        """Submit a Packing Slip print job. Structurally different from
        submit_report_job's JobTrav-derived Change<Key> pattern --
        Erp.Rpt.PackingSlipPrintSvc has no Change method at all. Epicor's own
        Kinetic client does this instead (confirmed by reading
        exports/System-Apps/Apps/Erp.UIRpt.PackingSlipPrint/events.jsonc, not
        guessed): GetNewParameters returns a single blank PackingSlipParam row
        with RowMod="A" (not JobTrav's two-row ""/"U" pair -- there's only
        ever one row here, so there's no existing row to "update"), then set
        PackNum on it, then PackNumDefaults(packNum, ds) to populate
        style-specific defaults, then SubmitToAgent -- RowMod stays "A"
        throughout, verified live. style_num defaults to 2 ("Standard - SSRS",
        SystemFlag=true) rather than JobTrav's 1001 convention -- 1001 here is
        a tenant custom style that was missing its RDL
        files (StatusCode=1) when this was verified against a test environment, so 2 is the
        safe default; callers should still pass the style verified for their
        own tenant. maint_program's value isn't documented anywhere -- it's
        inferred from the same Kinetic app-ID transform JobTrav's own
        maint_program follows (Erp.UIRpt.PackingSlipPrint ->
        Erp.UI.Rpt.PackingSlipPrintTransaction) and confirmed live:
        SubmitToAgent 400s with "You must pass the name of the maintenance
        program" when blank, and succeeds with this value (full live
        end-to-end verification against a test environment: PackNum=1, real PDF
        downloaded, magic bytes and size confirmed)."""
        new_params = self._rpt_call("Erp.Rpt.PackingSlipPrintSvc", "GetNewParameters", {}, company=company)
        ds = new_params.get("returnObj") or {}
        param_rows = ds.get("PackingSlipParam") or []
        if not param_rows:
            raise ValueError("GetNewParameters returned no PackingSlipParam row")
        param_rows[0]["PackNum"] = pack_num
        param_rows[0]["ReportStyleNum"] = style_num
        param_rows[0]["StyleNumExt"] = style_num
        param_rows[0]["WorkstationID"] = workstation_id

        defaults_resp = self._rpt_call(
            "Erp.Rpt.PackingSlipPrintSvc", "PackNumDefaults",
            {"packNum": pack_num, "ds": ds}, company=company,
        )
        result_ds = defaults_resp.get("parameters", {}).get("ds") or defaults_resp.get("parameters") or defaults_resp
        result_rows = result_ds.get("PackingSlipParam") or []
        if result_rows:
            result_rows[0]["AutoAction"] = "SSRSPREVIEW"
            result_rows[0]["SSRSRenderFormat"] = "PDF"

        submit_payload = {
            "ds": result_ds,
            "agentID": agent_id,
            "agentSchedNum": 0,
            "agentTaskNum": 0,
            "recurringTask": False,
            "maintProgram": maint_program,
        }
        return self._rpt_call("Erp.Rpt.PackingSlipPrintSvc", "SubmitToAgent", submit_payload, company=company)

    def get_monitor_tasks(self, workstation_id: str, task_description: Optional[str] = None, company: str = "") -> Dict[str, Any]:
        """Poll Ice.BO.SysMonitorSvc for task/report status. Started from
        tap_prod's monitorJobTraveler action, but verified live that its
        historyOnly=False/retrieveAllTasks=False combination is fragile: a
        completed task's SysRptLst row (needed to get the SysRowID for
        download) disappears from that "live" view almost immediately after
        completion -- confirmed by polling again seconds later and getting
        an empty SysRptLst for a task SysTask still correctly reports as
        COMPLETE. historyOnly=True + retrieveAllTasks=True reliably returns
        it. Also verified: workstationId in the request does NOT reliably
        scope results server-side -- tasks submitted under other workstation
        IDs showed up too -- so correlate a specific download by SysTaskNum
        (see download logic in callers), not by assuming the result set is
        already scoped to this workstation."""
        from datetime import datetime, timezone

        started_on = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00.000Z")
        payload = {
            "querySysRptLst": True,
            "workstationId": workstation_id,
            "querySysTask": True,
            "querySysTaskLog": True,
            "historyOnly": True,
            "startedOn": started_on,
            "retrieveAllTasks": True,
        }
        response = self._rpt_call("Ice.BO.SysMonitorSvc", "GetMonitorDataKeepIdleTime", payload, company=company)
        return_obj = response.get("returnObj") or response.get("parameters", {}).get("returnObj") or {}
        all_sys_task = return_obj.get("SysTask") or []
        task_by_num = {t.get("SysTaskNum"): t for t in all_sys_task}

        # SysTask rows carry no WorkStationID at all (verified live) -- only
        # SysRptLst does. Scope by workstation there first, then use each
        # row's SysTaskNum to pull its matching SysTask entry -- this is the
        # only reliable way to scope a result to "my" submission. Filtering
        # SysTask directly by task_description alone (with no workstation
        # scoping at all, since it's not possible) would cross-contaminate
        # between concurrent/sequential runs sharing the same description.
        sys_rpt_lst = [r for r in (return_obj.get("SysRptLst") or []) if r.get("WorkStationID") == workstation_id]
        if task_description:
            sys_rpt_lst = [r for r in sys_rpt_lst if r.get("RptDescription") == task_description]
        scoped_tasks = [task_by_num[r["SysTaskNum"]] for r in sys_rpt_lst if r.get("SysTaskNum") in task_by_num]

        return {
            "tasks": scoped_tasks,
            "sys_rpt_lst": sys_rpt_lst,
            "completed": [t for t in scoped_tasks if t.get("TaskStatus") == "COMPLETE"],
            "pending": [t for t in scoped_tasks if t.get("TaskStatus") not in ("COMPLETE", "ERROR")],
            "errored": [t for t in scoped_tasks if t.get("TaskStatus") == "ERROR"],
        }

    def wait_for_report_completion(
        self, workstation_id: str, task_description: str, timeout: float = 60.0, poll_interval: float = 3.0, company: str = ""
    ) -> Dict[str, Any]:
        """Block until a submitted report task completes, errors, or the
        timeout elapses -- the polling loop needed to use submit_report_job
        synchronously (e.g. in a CI/CD smoke test)."""
        import time

        deadline = time.monotonic() + timeout
        while True:
            status = self.get_monitor_tasks(workstation_id, task_description=task_description, company=company)
            if status["completed"] or status["errored"]:
                return status
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Report task '{task_description}' for workstation '{workstation_id}' "
                    f"did not complete within {timeout}s (pending: {len(status['pending'])})"
                )
            time.sleep(poll_interval)

    def download_report_pdf(self, sys_row_id: str, company: str = "") -> bytes:
        """Download a completed report's PDF bytes via
        Ice.BO.ReportMonitorSvc/GetReportBytes. The base64 field's exact
        nesting in the response isn't fixed (ported defensive search, see
        _find_base64_field), consistent with the verified TypeScript
        reference this was ported from."""
        response = self._rpt_call("Ice.BO.ReportMonitorSvc", "GetReportBytes", {"sysRowId": sys_row_id}, company=company)
        field = _find_base64_field(response)
        if not field:
            raise ValueError(f"Could not locate base64 PDF bytes in GetReportBytes response for sysRowId={sys_row_id}")
        return base64.b64decode(field["value"])

    def upload_file_to_server(self, local_path: str, server_path: str, folder: int = 4) -> bool:
        """
        Uploads a file using Ice.Lib.FileTransferSvc.
        
        Args:
            local_path: Path to the local file.
            server_path: Destination path on the server (e.g., '_TempZip//Reports.zip').
            folder: Epicor SpecialFolder enum value (default 4 = UserData/Temporary).
        """
        if not os.path.exists(local_path):
            print(f"❌ Local file not found: {local_path}")
            return False

        with open(local_path, "rb") as f:
            file_bytes = f.read()
            b64_data = base64.b64encode(file_bytes).decode('utf-8')

        endpoint = f"{self.config['url'].rstrip('/')}/api/v2/odata/{self.config['company']}/Ice.Lib.FileTransferSvc/UploadFile"
        
        payload = {
            "folder": folder,
            "serverPath": server_path,
            "data": b64_data
        }

        # FileTransferSvc often requires specific headers or just standard auth
        headers = self.mgr.get_auth_headers(self.config)

        print(f"Uploading {os.path.basename(local_path)} to {server_path}...")
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=300)

        if not resp.ok:
            self.log_wire("POST", endpoint, headers, payload, resp)
            print(f"❌ Upload failed: {resp.status_code} {resp.text}")
            return False
        
        print("✅ File uploaded successfully.")
        return True

    def extract_and_upload_reports_zip(self, server_path: str, report_id: str) -> bool:
        """
        Calls Ice.BO.ReportSvc/ExtractAndUploadReportsZip using the server file path.
        """
        endpoint = f"{self.config['url'].rstrip('/')}/api/v2/odata/{self.config['company']}/Ice.BO.ReportSvc/ExtractAndUploadReportsZip"
        
        # Payload references the file path on the server (uploaded via FileTransferSvc)
        payload = {
            "printProgram": report_id,
            "data": server_path
        }

        headers = self.mgr.get_auth_headers(self.config)
        
        print(f"Extracting and deploying reports from server path: {server_path}...")
        resp = requests.post(endpoint, json=payload, headers=headers, timeout=600)

        if not resp.ok:
            self.log_wire("POST", endpoint, headers, payload, resp)
            print(f"❌ Extraction failed: {resp.status_code} {resp.text}")
            return False

        print("✅ Reports extracted and uploaded successfully.")
        return True

def _build_job_trav_param(job_num: str, style_num: int, workstation_id: str) -> Dict[str, Any]:
    """The full JobTravParam row schema, verified live against a test environment (see
    submit_report_job's docstring for provenance) -- field list and
    defaults are specific to Erp.Rpt.JobTravSvc, not a generic report
    param shape."""
    return {
        "PrntAllMassPrnt": False, "Jobs": job_num, "Assembly": "", "SubAssem": False, "NewPgPerAsm": False,
        "OprDates": False, "OprStd": False, "BarCodes": True, "ShpSchd": False,
        "PrintSchedResources": False, "PrintSchedResrcDesc": False, "OpInstructions": False,
        "PrintAttributes": False, "DisableShpSchd": False,
        "SysRowID": "00000000-0000-0000-0000-000000000000",
        "AutoAction": "SSRSPREVIEW", "PrinterName": "", "AgentSchedNum": 0, "AgentID": "", "AgentTaskNum": 0,
        "RecurringTask": False, "RptPageSettings": "", "RptPrinterSettings": "", "RptVersion": "",
        "ReportStyleNum": style_num, "WorkstationID": workstation_id, "TaskNote": "", "ArchiveCode": 0,
        "DateFormat": "m/d/yyyy", "NumericFormat": ",.", "AgentCompareString": "",
        "ProcessID": "", "ProcessCompany": "", "ProcessSystemCode": "", "ProcessTaskNum": 0,
        "DecimalsGeneral": 0, "DecimalsCost": 0, "DecimalsPrice": 0,
        "GlbDecimalsGeneral": 0, "GlbDecimalsCost": 0, "GlbDecimalsPrice": 0,
        "FaxSubject": "", "FaxTo": "", "FaxNumber": "",
        "EMailTo": "", "EMailCC": "", "EMailBCC": "", "EMailBody": "",
        "AttachmentType": "", "ReportCurrencyCode": "USD", "ReportCultureCode": "en-US",
        "SSRSRenderFormat": "PDF", "UIXml": "", "PrintReportParameters": False,
        "SSRSEnableRouting": False, "DesignMode": False,
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Kinetic Report Service CLI: deploy report packages and render reports to PDF.\n"
            "Actions:\n"
            "  upload              WRITES TO SERVER: upload FILE via Ice.Lib.FileTransferSvc to --server-path\n"
            "  extract             WRITES TO EPICOR: run Ice.BO.ReportSvc/ExtractAndUploadReportsZip on a zip\n"
            "                      already at --server-path, (re)deploying the --report-id reports\n"
            "  deploy              upload, then extract (both of the above)\n"
            "  print-job-traveler  submit an Erp.Rpt.JobTravSvc job for --job-num to the System Task Agent,\n"
            "                      wait for it, and download the PDF\n"
            "  print-packing-slip  same for Erp.Rpt.PackingSlipPrintSvc and --pack-num\n"
            "Print actions write the PDF directly to --out (an existing file is replaced)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m kinetic_devops.report_service deploy Reports.zip --env MyTestEnv "
            "--report-id reports/CustomReports/PackingSlip/PackSlip\n"
            "  python -m kinetic_devops.report_service print-packing-slip --env MyTestEnv --pack-num 123 --out slip.pdf"
        ),
    )
    parser.add_argument(
        "action", choices=['upload', 'extract', 'deploy', 'print-job-traveler', 'print-packing-slip'],
        help="Action to perform (see above)",
    )
    parser.add_argument("file", nargs="?", metavar="FILE", help="Local path to the .zip file (upload/deploy)")
    parser.add_argument(
        "--server-path", metavar="PATH",
        help="Server-side path of the uploaded zip, in the user-data special folder (upload/extract/deploy; "
             "default: %(default)s)",
        default="_TempZip//Reports.zip",
    )
    parser.add_argument(
        "--report-id", metavar="PRINT_PROGRAMS",
        help="Report ID (printProgram) path(s) to deploy, comma-separated, e.g. "
             "reports/CustomReports/PackingSlip/PackSlip,reports/CustomReports/ShippingLabels/ShipLabl "
             "(required for extract/deploy)",
    )
    parser.add_argument("--job-num", metavar="JOB", help="JobNum to print a traveler for (required for print-job-traveler)")
    parser.add_argument(
        "--pack-num", metavar="PACK", type=int,
        help="PackNum to print a packing slip for (required for print-packing-slip)",
    )
    parser.add_argument(
        "--style-num", metavar="N", type=int,
        help="ReportStyleNum (print-job-traveler default: 1001, print-packing-slip default: 2)",
    )
    parser.add_argument(
        "--workstation-id", metavar="ID", default="kinetic-devops",
        help="WorkstationID used to correlate the submitted task (default: %(default)s)",
    )
    parser.add_argument(
        "--out", metavar="PDF",
        help="Output PDF path (default: <JobNum>.pdf or PackSlip-<PackNum>.pdf)",
    )
    parser.add_argument(
        "--wait-timeout", metavar="SECONDS", type=float, default=60.0,
        help="Seconds to wait for report completion (default: 60)",
    )
    parser.add_argument(
        "--env", metavar="ENV",
        help="Environment Nickname saved with 'auth store'. Default: KINETIC_SESSION_ENV, else the last "
             "active session or an interactive prompt.",
    )
    parser.add_argument("--user", metavar="USER_ID", help="Specific User ID (session) to use for ENV")
    parser.add_argument("--debug", action="store_true", help="Enable debug output and print tracebacks on error.")

    args = parser.parse_args()

    try:
        service = KineticReportService(args.env, args.user, debug=args.debug)

        if args.action == 'print-job-traveler':
            if not args.job_num:
                print("❌ Error: --job-num is required for print-job-traveler.")
                sys.exit(1)
            style_num = args.style_num if args.style_num is not None else 1001
            base_param = _build_job_trav_param(args.job_num, style_num, args.workstation_id)
            service.submit_report_job(
                "Erp.Rpt.JobTravSvc", "ChangeJobNum", "JobTravParam", base_param,
                maint_program="Erp.UI.Rpt.JobTravTransaction",
            )
            status = service.wait_for_report_completion(
                args.workstation_id, "Job Traveler", timeout=args.wait_timeout
            )
            if status["errored"]:
                print(f"❌ Report task errored: {status['errored']}")
                sys.exit(1)
            # sys_rpt_lst is already scoped to this workstation+description by
            # get_monitor_tasks; take the most recently started match.
            if not status["sys_rpt_lst"]:
                print("❌ Completed, but no matching SysRptLst row found to download.")
                sys.exit(1)
            sys_row_id = sorted(status["sys_rpt_lst"], key=lambda r: r.get("CreatedOn") or "")[-1]["SysRowID"]
            pdf_bytes = service.download_report_pdf(sys_row_id)
            out_path = args.out or f"{args.job_num}.pdf"
            with open(out_path, "wb") as f:
                f.write(pdf_bytes)
            print(f"✅ Printed Job Traveler for {args.job_num} -> {out_path} ({len(pdf_bytes)} bytes)")
        elif args.action == 'print-packing-slip':
            if not args.pack_num:
                print("❌ Error: --pack-num is required for print-packing-slip.")
                sys.exit(1)
            style_num = args.style_num if args.style_num is not None else 2
            service.submit_packing_slip_job(args.pack_num, args.workstation_id, style_num=style_num)
            status = service.wait_for_report_completion(
                args.workstation_id, "Packing Slip Print", timeout=args.wait_timeout
            )
            if status["errored"]:
                print(f"❌ Report task errored: {status['errored']}")
                sys.exit(1)
            if not status["sys_rpt_lst"]:
                print("❌ Completed, but no matching SysRptLst row found to download.")
                sys.exit(1)
            sys_row_id = sorted(status["sys_rpt_lst"], key=lambda r: r.get("CreatedOn") or "")[-1]["SysRowID"]
            pdf_bytes = service.download_report_pdf(sys_row_id)
            out_path = args.out or f"PackSlip-{args.pack_num}.pdf"
            with open(out_path, "wb") as f:
                f.write(pdf_bytes)
            print(f"✅ Printed Packing Slip for PackNum {args.pack_num} -> {out_path} ({len(pdf_bytes)} bytes)")
        elif args.action == 'upload':
            service.upload_file_to_server(args.file, args.server_path)
        elif args.action == 'extract':
            if not args.report_id:
                print("❌ Error: --report-id is required for extraction.")
                sys.exit(1)
            service.extract_and_upload_reports_zip(args.server_path, args.report_id)
        elif args.action == 'deploy':
            if not args.report_id:
                print("❌ Error: --report-id is required for deployment.")
                sys.exit(1)
            if service.upload_file_to_server(args.file, args.server_path):
                service.extract_and_upload_reports_zip(args.server_path, args.report_id)

    except Exception as e:
        print(f"❌ Error: {e}")
        if args.debug:
            import traceback
            traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()