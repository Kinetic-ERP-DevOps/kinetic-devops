"""Tests for efx_library's package-based plan/deploy helpers.

Packages are synthetic but follow the live ExportLibrary shape; git
behavior is exercised against throwaway repositories, so nothing here needs
credentials or an Epicor environment.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from kinetic_devops.efx_library import (
    _git_commit_files,
    _git_show,
    build_package_from_target,
    decode_package,
    deployment_drift,
    diff_packages,
    encode_package,
    read_library_source,
    source_ref_accepted,
)


def _body(code: str, step_id: str = "11111111-2222-3333-4444-555555555555", name: str = "Execute Custom Code 0"):
    escaped = code.replace("&", "&amp;").replace('"', "&quot;").replace("\n", "&#xA;")
    return (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<DirectiveDefinition2 Version="3" xmlns="clr-namespace:Ice.Lib.Bpm.Model;assembly=Ice.Lib.Bpm.Shared" '
        'xmlns:ilbma="clr-namespace:Ice.Lib.Bpm.Model.Actions;assembly=Ice.Lib.Bpm.Shared" '
        'xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">'
        '<DirectiveDefinition2.StartNode>'
        f'<DirectiveStep Next="{{x:Null}}" DisplayName="{name}" Id="{step_id}">'
        f'<DirectiveStep.Action><ilbma:CustomCodeAction Code="{escaped}" /></DirectiveStep.Action>'
        '</DirectiveStep>'
        '</DirectiveDefinition2.StartNode>'
        '</DirectiveDefinition2>'
    )


def _package(code="var a = 1;", **library_overrides):
    library = {
        "OriginalID": "LIB-A",
        "GlobalID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "EpicorVersion": "3.2.600",
        "Revision": 7,
        "Published": True,
        "Disabled": False,
        "Owner": "OWNER1",
        "OwnedByCompany": "CO1",
        "Description": "old library description",
        "Functions": [{
            "FunctionID": "FN-A", "Description": "old fn description", "Kind": 1,
            "RequireTransaction": False, "SingleRowMode": False, "Private": False, "Disabled": False,
            "Invalid": False, "Thumbnail": "png-bytes", "Body": _body(code),
        }],
        "LibraryReferences": [{"ReferenceType": 1, "ReferenceID": "ERP.Company", "Updatable": False}],
        "LibraryMappings": [{"Company": "CO1", "Allowed": True}, {"Company": "CO2", "Allowed": True}],
    }
    library.update(library_overrides)
    return {"Mode": "Backup", "Version": "5.2.100", "SystemCode": "ERP", "LibraryId": "LIB-A", "Library": library}


def _source(code="var a = 2;", file_name="Execute_Custom_Code_0.cs", library=None, meta_extra=None):
    meta = {"FunctionID": "FN-A", "Description": "new fn description", "Kind": 1,
            "RequireTransaction": False, "SingleRowMode": False, "Private": False}
    meta.update(meta_extra or {})
    return {"library": library if library is not None else {"Description": "new library description"},
            "functions": {"FN-A": {"meta": meta, "files": {file_name: code}}}}


DEPLOYMENT = {"target": {"LibraryID": "LIB-A", "Published": True, "Disabled": False, "Owner": "OWNER1",
                         "OwnedByCompany": "CO1", "SharedWithGroup": "GroupX",
                         "LibraryMappings": [{"Company": "CO1", "Allowed": True}, {"Company": "CO2", "Allowed": True}]},
              "compatibility": {"Version": "5.2.100", "EpicorVersion": "3.2.600", "override": None}}


class TestPackageEncoding(unittest.TestCase):
    def test_encode_starts_with_bom_and_round_trips(self):
        package = _package()
        data = encode_package(package)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(decode_package(data), package)

    def test_decode_accepts_base64_export_output(self):
        import base64

        package = _package()
        self.assertEqual(decode_package(base64.b64encode(encode_package(package)).decode()), package)


class TestBuildPackageFromTarget(unittest.TestCase):
    def test_applies_source_code_and_metadata_and_keeps_target_ids(self):
        target = _package()
        built, notes = build_package_from_target(target, _source(), DEPLOYMENT)
        library = built["Library"]
        self.assertEqual(library["GlobalID"], target["Library"]["GlobalID"])
        self.assertEqual(library["OriginalID"], "LIB-A")
        self.assertEqual(library["Description"], "new library description")
        self.assertEqual(library["SharedWithGroup"], "GroupX")
        self.assertEqual(library["Functions"][0]["Description"], "new fn description")
        self.assertIn('Code="var a = 2;"', library["Functions"][0]["Body"])
        self.assertEqual(notes, [])
        self.assertIn('Code="var a = 1;"', target["Library"]["Functions"][0]["Body"], "input must not be mutated")

    def test_null_env_field_is_removed_like_an_export(self):
        deployment = json.loads(json.dumps(DEPLOYMENT))
        deployment["target"]["SharedWithGroup"] = None
        built, _ = build_package_from_target(_package(SharedWithGroup="GroupX"), _source(), deployment)
        self.assertNotIn("SharedWithGroup", built["Library"])

    def test_matches_older_guid_prefixed_file_layout(self):
        source = _source(file_name="11111111-2222-3333-4444-555555555555__Execute_Custom_Code_0.cs")
        built, notes = build_package_from_target(_package(), source, DEPLOYMENT)
        self.assertIn('Code="var a = 2;"', built["Library"]["Functions"][0]["Body"])
        self.assertEqual(notes, [])

    def test_matches_nodes_map_by_display_name(self):
        source = _source(file_name="renamed.cs",
                         meta_extra={"Nodes": [{"DisplayName": "Execute Custom Code 0", "File": "renamed.cs"}]})
        built, _ = build_package_from_target(_package(), source, DEPLOYMENT)
        self.assertIn('Code="var a = 2;"', built["Library"]["Functions"][0]["Body"])

    def test_declared_references_replace_the_target_set(self):
        refs = [{"ReferenceType": 1, "ReferenceID": "ERP.Part", "Updatable": False}]
        built, _ = build_package_from_target(_package(), _source(library={"References": refs}), DEPLOYMENT)
        self.assertEqual(built["Library"]["LibraryReferences"], refs)

    def test_undeclared_references_keep_the_target_set(self):
        target = _package()
        built, _ = build_package_from_target(target, _source(), DEPLOYMENT)
        self.assertEqual(built["Library"]["LibraryReferences"], target["Library"]["LibraryReferences"])

    def test_function_only_in_source_is_rejected(self):
        source = _source()
        source["functions"]["FN-NEW"] = {"meta": {"FunctionID": "FN-NEW"}, "files": {}}
        with self.assertRaises(ValueError):
            build_package_from_target(_package(), source, DEPLOYMENT)

    def test_compatibility_override_stamps_versions(self):
        deployment = json.loads(json.dumps(DEPLOYMENT))
        deployment["compatibility"]["override"] = {"Version": "9.9.999", "EpicorVersion": "9.9.9", "reason": "test"}
        built, notes = build_package_from_target(_package(), _source(), deployment)
        self.assertEqual(built["Version"], "9.9.999")
        self.assertEqual(built["Library"]["EpicorVersion"], "9.9.9")
        self.assertTrue(any("override" in n for n in notes))


class TestDiffPackages(unittest.TestCase):
    def test_identical_packages_have_no_changes(self):
        self.assertEqual(diff_packages(_package(), _package()), [])

    def test_noise_fields_are_ignored(self):
        after = _package(Revision=99)
        after["Library"]["Functions"][0]["Thumbnail"] = "different"
        self.assertEqual(diff_packages(_package(), after), [])

    def test_reports_code_reference_mapping_and_parameter_changes(self):
        after = _package(code="var a = 2;\nvar b = 3;",
                         LibraryReferences=[{"ReferenceType": 1, "ReferenceID": "ERP.Part", "Updatable": False}],
                         LibraryMappings=[{"Company": "CO1", "Allowed": True}])
        after["Library"]["Functions"][0]["FunctionSignature"] = [{"ArgumentName": "dryRun"}]
        changes = diff_packages(_package(), after)
        self.assertIn("reference removed: ERP.Company", changes)
        self.assertIn("reference added: ERP.Part", changes)
        self.assertIn("company mapping CO2: True -> None", changes)
        self.assertIn("FN-A parameters: [] -> ['dryRun']", changes)
        self.assertIn("FN-A/Execute Custom Code 0 code: 1 -> 2 lines", changes)


class TestDeploymentDrift(unittest.TestCase):
    def test_no_drift_when_record_matches_live(self):
        record = json.loads(json.dumps(DEPLOYMENT))
        record["target"]["GlobalID"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.assertEqual(deployment_drift(record, _package(SharedWithGroup="GroupX")), [])

    def test_reports_changes_made_outside_the_pipeline(self):
        drift = deployment_drift(DEPLOYMENT, _package(Published=False))
        self.assertTrue(any(line.startswith("Published") for line in drift))
        self.assertTrue(any(line.startswith("SharedWithGroup") for line in drift))


class _TempRepo(unittest.TestCase):
    def setUp(self):
        # KINETIC_TEST_TMPDIR (optional) relocates these throwaway repos, e.g.
        # off %TEMP% where endpoint security can stall short-lived git processes.
        tmp_root = os.environ.get("KINETIC_TEST_TMPDIR") or None
        if tmp_root:
            os.makedirs(tmp_root, exist_ok=True)
        self.repo = tempfile.mkdtemp(prefix="efx-deploy-test-", dir=tmp_root)
        self.git("init", "-q", "-b", "env/root")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.write("README.md", "root\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "root")

    def tearDown(self):
        def make_writable_and_retry(func, path, _exc):
            # git marks object files read-only, which blocks deletion on Windows.
            os.chmod(path, stat.S_IWRITE)
            func(path)

        if sys.version_info >= (3, 12):
            shutil.rmtree(self.repo, onexc=make_writable_and_retry)
        else:
            shutil.rmtree(self.repo, onerror=lambda func, path, exc_info: make_writable_and_retry(func, path, exc_info))

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


class TestGitCommitFiles(_TempRepo):
    def test_creates_branch_from_start_point_without_touching_worktree(self):
        status_before = self.git("status", "--porcelain")
        commit = _git_commit_files(self.repo, "snapshot/x", {"a/b.efxlib": b"\xef\xbb\xbf{}"}, "msg", "env/root")
        self.assertEqual(self.git("rev-parse", "snapshot/x").strip(), commit)
        self.assertEqual(_git_show(self.repo, "snapshot/x", "a/b.efxlib"), b"\xef\xbb\xbf{}")
        self.assertEqual(_git_show(self.repo, "snapshot/x", "README.md"), b"root\n")
        self.assertEqual(self.git("status", "--porcelain"), status_before)
        self.assertEqual(self.git("branch", "--show-current").strip(), "env/root")

    def test_appends_to_existing_branch(self):
        first = _git_commit_files(self.repo, "env/x", {"one.json": b"1"}, "first", "env/root")
        second = _git_commit_files(self.repo, "env/x", {"two.json": b"2"}, "second", "env/root")
        self.assertEqual(self.git("rev-parse", "env/x~1").strip(), first)
        self.assertEqual(self.git("rev-parse", "env/x").strip(), second)
        self.assertEqual(_git_show(self.repo, "env/x", "one.json"), b"1")


class TestReadLibrarySourceAndPolicy(_TempRepo):
    def test_reads_library_and_functions_at_ref(self):
        self.write("LIB-A/_library.json", json.dumps({"Description": "d"}))
        self.write("LIB-A/FN-A/_meta.json", json.dumps({"FunctionID": "FN-A", "Description": "f"}))
        self.write("LIB-A/FN-A/Execute_Custom_Code_0.cs", "var a = 2;")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "source")
        source = read_library_source(self.repo, "HEAD", "LIB-A")
        self.assertEqual(source["library"], {"Description": "d"})
        self.assertEqual(source["functions"]["FN-A"]["files"], {"Execute_Custom_Code_0.cs": "var a = 2;"})

    def test_missing_ref_raises(self):
        with self.assertRaises(ValueError):
            read_library_source(self.repo, "0" * 40, "LIB-A")

    def test_accepted_sources_match_branches_containing_the_ref(self):
        self.git("branch", "main")
        self.git("switch", "-q", "-c", "feature/x")
        self.write("f.txt", "x")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "feature")
        feature_ref = self.git("rev-parse", "HEAD").strip()
        self.assertEqual(source_ref_accepted(self.repo, feature_ref, ["feature/*"]), "feature/x")
        self.assertIsNone(source_ref_accepted(self.repo, feature_ref, ["main"]))
        root_ref = self.git("rev-parse", "main").strip()
        self.assertEqual(source_ref_accepted(self.repo, root_ref, ["main"]), "main")


if __name__ == "__main__":
    unittest.main()
