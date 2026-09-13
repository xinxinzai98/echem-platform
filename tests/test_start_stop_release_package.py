import hashlib
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.package_start_stop_release import package
from start_stop_service import _sanitize_job_result


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repository"
        self.repo.mkdir()
        self.git("init", "--quiet")
        (self.repo / "Dockerfile").write_text("FROM example@sha256:fixture\n")
        (self.repo / "start_stop_service.py").write_text("# source fixture\n")
        self.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")

    def test_archive_uses_commit_and_every_file_hash_matches(self):
        (self.repo / "untracked-private.txt").write_text("must not ship")
        result = package(self.repo, self.root / "out", "0.7.0-dev.1")
        archive_path = Path(result["archive"])
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), result["archive_sha256"])
        with tarfile.open(archive_path) as archive:
            self.assertNotIn("untracked-private.txt", archive.getnames())
            manifest_bytes = archive.extractfile("RELEASE-MANIFEST.json").read()
            self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), result["manifest_sha256"])
            manifest = json.loads(manifest_bytes)
            self.assertEqual(manifest["git_commit"], self.git("rev-parse", "HEAD").decode().strip())
            for item in manifest["files"]:
                content = archive.extractfile(item["path"]).read()
                self.assertEqual(len(content), item["bytes"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
        with self.assertRaises(FileExistsError):
            package(self.repo, self.root / "out", "0.7.0-dev.1")

    def test_dirty_and_untracked_runtime_refuse_packaging(self):
        (self.repo / "start_stop_service.py").write_text("# uncommitted change\n")
        with self.assertRaisesRegex(RuntimeError, "Commit release runtime"):
            package(self.repo, self.root / "out", "0.7.0-dev.1")
        self.commit()
        (self.repo / "static").mkdir()
        (self.repo / "static/start-stop-new.js").write_text("// new runtime\n")
        with self.assertRaisesRegex(RuntimeError, "Commit release runtime"):
            package(self.repo, self.root / "out", "0.7.0-dev.1")

    def test_tracked_database_is_rejected_and_no_partial_archive_remains(self):
        (self.repo / "private.sqlite3").write_bytes(b"private")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Unexpected secret, database"):
            package(self.repo, self.root / "out", "0.7.0-dev.1")
        self.assertEqual(list((self.root / "out").iterdir()), [])

    def test_empty_line_placeholder_allowed_but_state_content_rejected(self):
        (self.repo / "state").mkdir()
        keep = self.repo / "state/.gitkeep"
        keep.write_bytes(b"\n")
        self.commit()
        package(self.repo, self.root / "out", "0.7.0-dev.1")
        keep.write_bytes(b"private")
        self.commit()
        with self.assertRaisesRegex(ValueError, "Private state"):
            package(self.repo, self.root / "out", "0.7.0-dev.2")

    def test_job_mode_is_a_safe_enum_not_an_untrusted_string_or_object(self):
        self.assertEqual(_sanitize_job_result({"render_data_mode":"raw"}), {"render_data_mode":"raw"})
        for value in ("private path", {}, [], None):
            self.assertEqual(_sanitize_job_result({"render_data_mode":value}), {})
