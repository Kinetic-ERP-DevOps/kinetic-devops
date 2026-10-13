"""Regression tests for report_service.py's report-printing helpers.

The submit/monitor/download flow itself (KineticReportService.submit_report_job,
submit_packing_slip_job, get_monitor_tasks, wait_for_report_completion,
download_report_pdf) is live-verified against a test environment -- full end-to-end PDF
downloads of a real Job Traveler report and a real Packing Slip (PackNum 1,
StyleNum 2) -- not mocked here -- these tests cover the pure logic pieces
that don't need live credentials.
"""

import unittest

from kinetic_devops.report_service import _build_job_trav_param, _find_base64_field


class TestFindBase64Field(unittest.TestCase):
    def test_finds_long_string_at_top_level(self):
        data = {"short": "x", "long": "a" * 150}
        result = _find_base64_field(data)
        self.assertEqual(result["path"], "long")
        self.assertEqual(result["value"], "a" * 150)

    def test_finds_long_string_nested(self):
        data = {"parameters": {"returnObj": {"fileData": "b" * 200}}}
        result = _find_base64_field(data)
        self.assertEqual(result["path"], "parameters.returnObj.fileData")

    def test_ignores_short_strings(self):
        data = {"a": "short", "b": "also short"}
        self.assertIsNone(_find_base64_field(data))

    def test_returns_none_for_empty_dict(self):
        self.assertIsNone(_find_base64_field({}))

    def test_returns_first_match_depth_first(self):
        data = {"first": "c" * 150, "second": "d" * 150}
        result = _find_base64_field(data)
        self.assertEqual(result["path"], "first")


class TestBuildJobTravParam(unittest.TestCase):
    def test_sets_job_and_style_and_workstation(self):
        param = _build_job_trav_param("JOB-123", 1005, "my-ws")
        self.assertEqual(param["Jobs"], "JOB-123")
        self.assertEqual(param["ReportStyleNum"], 1005)
        self.assertEqual(param["WorkstationID"], "my-ws")

    def test_forces_pdf_preview_render(self):
        param = _build_job_trav_param("JOB-123", 1001, "ws")
        self.assertEqual(param["AutoAction"], "SSRSPREVIEW")
        self.assertEqual(param["SSRSRenderFormat"], "PDF")


if __name__ == "__main__":
    unittest.main()
