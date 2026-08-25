from __future__ import annotations

import base64
import contextlib
import datetime as dt
import io
import json
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from echem_platform.start_stop_collection import (
    CollectionError,
    CredentialConfigurationError,
    LocalDatabaseError,
    LocalFilesystemError,
    LocalProgramError,
    LocalStorageError,
    RemoteFileError,
    RemoteRootError,
    SSHWindowsTransport,
    STREAM_VERIFICATION_PREFIX,
    SourceChangedError,
    StartStopCollector,
    UnexpectedCollectionError,
    UTC,
    inventory_script,
    main as collection_main,
)


def utc_text(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeDatabase:
    def __init__(self) -> None:
        self.batch_id = "batch-test"
        self.current: dict[tuple[str, str, str], tuple[int, int]] = {}
        self.ingested: list[dict] = []
        self.issues: list[dict] = []
        self.finished: list[dict] = []

    def begin_collection_batch(self, *, machine_count=0, metadata=None, batch_id=None):
        if batch_id:
            self.batch_id = str(batch_id)
        self.begin = {"machine_count": machine_count, "metadata": metadata}
        return self.batch_id

    def needs_download(self, machine_id, root_label, remote_path, size, ticks):
        return self.current.get(
            (machine_id, root_label, remote_path.casefold())
        ) != (size, ticks)

    def ingest_staged_file(self, path, metadata, batch_id):
        self.assert_batch(batch_id)
        raw = Path(path).read_bytes()
        record = {"metadata": dict(metadata), "bytes": raw}
        self.ingested.append(record)
        key = (
            metadata["machine_id"],
            metadata["root_label"],
            metadata["remote_path"].casefold(),
        )
        self.current[key] = (metadata["size"], metadata["last_write_ticks"])
        return {
            "changed": True,
            "status": "ingested",
            "source_id": len(self.ingested),
            "version_id": len(self.ingested),
            "size_bytes": len(raw),
        }

    def record_collection_issue(self, batch_id, **issue):
        self.assert_batch(batch_id)
        self.issues.append(issue)
        return len(self.issues)

    def finish_collection_batch(self, batch_id, *, status="completed", totals=None):
        self.assert_batch(batch_id)
        self.finished.append({"status": status, "totals": dict(totals or {})})
        return {"batch_id": batch_id, "snapshot_id": "snapshot-test"}

    def assert_batch(self, batch_id):
        if batch_id != self.batch_id:
            raise AssertionError(f"wrong batch: {batch_id}")


class FakeTransport:
    def __init__(self, inventories, contents) -> None:
        self.inventories = inventories
        self.contents = contents
        self.inventory_errors: set[tuple[str, str]] = set()
        self.fetch_errors: set[str] = set()
        self.stat_sequences: dict[str, list[dict]] = {}
        self.inventory_calls: list[tuple[str, str]] = []
        self.stat_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.max_partial_files = 0

    def inventory_root(self, machine, root, extensions):
        key = (machine["id"], root["label"])
        self.inventory_calls.append(key)
        if key in self.inventory_errors:
            raise RemoteRootError("实验电脑数据源读取失败")
        return self.inventories[key]

    def stat_file(self, machine, remote_path):
        self.stat_calls.append(remote_path)
        sequence = self.stat_sequences.get(remote_path)
        if sequence:
            return sequence.pop(0)
        for inventory in self.inventories.values():
            for record in inventory["files"]:
                if record["path"].casefold() == remote_path.casefold():
                    return {
                        "exists": True,
                        "size": record["size"],
                        "last_write_ticks": record["last_write_ticks"],
                        "last_write_utc": record["last_write_utc"],
                    }
        return {"exists": False}

    def fetch_file(self, machine, remote_path, destination, expected_size):
        self.fetch_calls.append(remote_path)
        if remote_path in self.fetch_errors:
            raise RemoteFileError("远端文件读取失败")
        partial_count = len(list(destination.parent.glob("*.part")))
        self.max_partial_files = max(self.max_partial_files, partial_count + 1)
        Path(destination).write_bytes(self.contents[remote_path])


class CollectorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scratch = self.root / "scratch"
        self.now = dt.datetime(2026, 8, 3, 6, 0, tzinfo=UTC)
        self.machine = {
            "id": "machine-a",
            "hostname": "A-9",
            "ip": "192.168.110.164",
            "user": "89464",
            "identity_file": "/run/secrets/machine-a-key",
            "roots": [
                {
                    "label": "desktop-data",
                    "remote_path": "C:\\Users\\89464\\Desktop\\data",
                    "exclude_directories": [],
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def record(
        self,
        name="启停.txt",
        *,
        content=b"time,potential\n0,-0.5\n",
        age_minutes=30,
        ticks=638898000000000000,
    ):
        modified = self.now - dt.timedelta(minutes=age_minutes)
        remote_path = self.machine["roots"][0]["remote_path"] + "\\" + name
        return (
            {
                "path": remote_path,
                "relative": name,
                "size": len(content),
                "last_write_ticks": ticks,
                "last_write_utc": utc_text(modified),
            },
            content,
        )

    def inventory(self, records):
        return {
            "exists": True,
            "scanned_at_utc": utc_text(self.now),
            "files": list(records),
        }

    def config(self, machine=None):
        return {
            "settle_seconds": 900,
            "extensions": [".txt", ".csv"],
            "machines": [machine or self.machine],
        }

    def collector(self, database, transport):
        return StartStopCollector(
            database,
            self.scratch,
            transport=transport,
            retry_attempts=1,
            retry_delay_seconds=0,
            reserved_free_bytes=0,
        )

    def test_collection_batch_records_search_location_revision(self):
        database = FakeDatabase()
        transport = FakeTransport(
            {(self.machine["id"], "desktop-data"): self.inventory([])},
            {},
        )
        config = self.config()
        config["collection_config_revision"] = 7

        summary = self.collector(database, transport).collect(
            config,
            batch_id="parent-generated-batch",
        )

        self.assertEqual(summary["collection_config_revision"], 7)
        self.assertEqual(summary["batch_id"], "parent-generated-batch")
        self.assertEqual(
            database.begin["metadata"]["collection_config_revision"], 7
        )

    def test_remote_inventory_prunes_excluded_directories_before_descending(self):
        script = inventory_script(
            "C:\\data",
            [".txt"],
            ["历史结果", "备份"],
            True,
        )

        self.assertIn("System.Collections.Generic.Stack[string]", script)
        self.assertIn("$excludedDirectories -contains $directory.Name", script)
        self.assertIn("[IO.FileAttributes]::ReparsePoint", script)
        self.assertNotIn("-Recurse", script)

    def test_three_machine_inventories_are_started_in_parallel(self):
        second_machine = {
            **self.machine,
            "id": "machine-b",
            "hostname": "AGHID-H",
            "ip": "192.168.110.155",
            "roots": [dict(self.machine["roots"][0])],
        }
        barrier = threading.Barrier(2, timeout=2)

        class ParallelInventoryTransport(FakeTransport):
            def inventory_root(inner_self, machine, root, extensions):
                del extensions
                key = (machine["id"], root["label"])
                inner_self.inventory_calls.append(key)
                barrier.wait()
                return inner_self.inventories[key]

        inventories = {
            (self.machine["id"], "desktop-data"): self.inventory([]),
            (second_machine["id"], "desktop-data"): self.inventory([]),
        }
        transport = ParallelInventoryTransport(inventories, {})

        summary = self.collector(FakeDatabase(), transport).collect(
            self.config(machine=self.machine) | {"machines": [self.machine, second_machine]}
        )

        self.assertEqual(summary["totals"]["roots_succeeded"], 2)
        self.assertEqual(len(transport.inventory_calls), 2)

    def test_stable_file_is_revalidated_before_and_after_then_ingested(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([record])}, {record["path"]: content})
        database = FakeDatabase()

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["snapshot_id"], "snapshot-test")
        self.assertEqual(summary["totals"]["downloaded"], 1)
        self.assertEqual(summary["totals"]["ingested"], 1)
        self.assertEqual(transport.stat_calls, [record["path"], record["path"]])
        self.assertEqual(database.ingested[0]["bytes"], content)
        self.assertEqual(
            database.ingested[0]["metadata"]["remote_last_write_utc"]
            if "remote_last_write_utc" in database.ingested[0]["metadata"]
            else database.ingested[0]["metadata"]["last_write_utc"],
            record["last_write_utc"],
        )
        self.assertFalse(any(self.scratch.glob("collection-*")))
        self.assertLessEqual(transport.max_partial_files, 1)

    def test_verified_transport_path_uses_one_transfer_without_extra_stats(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )
        verified_calls: list[str] = []

        def verified_fetch(machine, remote_path, destination, size, ticks):
            del machine
            self.assertEqual(size, record["size"])
            self.assertEqual(ticks, record["last_write_ticks"])
            verified_calls.append(remote_path)
            Path(destination).write_bytes(content)
            return {"size": size, "last_write_ticks": ticks}

        transport.fetch_file_verified = verified_fetch
        database = FakeDatabase()

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["totals"]["ingested"], 1)
        self.assertEqual(verified_calls, [record["path"]])
        self.assertEqual(transport.stat_calls, [])
        self.assertEqual(transport.fetch_calls, [])
        self.assertEqual(database.ingested[0]["bytes"], content)

    def test_progress_reports_safe_filename_and_real_collection_phases(self):
        record, content = self.record(name="子目录\\启停数据.txt")
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )
        database = FakeDatabase()
        events: list[dict] = []
        collector = StartStopCollector(
            database,
            self.scratch,
            transport=transport,
            retry_attempts=1,
            retry_delay_seconds=0,
            reserved_free_bytes=0,
            progress_callback=events.append,
        )

        collector.collect(self.config())

        phases = [event["phase"] for event in events]
        self.assertIn("connecting_remote", phases)
        self.assertIn("scanning_remote", phases)
        self.assertIn("downloading_remote", phases)
        self.assertIn("storing_database", phases)
        self.assertEqual(events[-1]["phase"], "collection_complete")
        self.assertEqual(events[-1]["percent"], 100)
        file_events = [
            event
            for event in events
            if event["phase"] in {"downloading_remote", "storing_database"}
        ]
        self.assertTrue(file_events)
        self.assertTrue(
            all(event["current_item"] == "启停数据.txt" for event in file_events)
        )
        scanning_file_events = [
            event
            for event in events
            if event["phase"] == "scanning_remote"
            and event["current_item"] == "启停数据.txt"
        ]
        self.assertTrue(scanning_file_events)
        transfer_phase_indexes = {
            event["phase_index"]
            for event in events
            if event["phase"] in {"downloading_remote", "storing_database"}
        }
        self.assertEqual(transfer_phase_indexes, {3})
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(self.machine["roots"][0]["remote_path"], serialized)
        self.assertNotIn(record["path"], serialized)
        self.assertEqual(events[0]["machines"][0]["machine_id"], "A-9")
        self.assertEqual(events[0]["machines"][0]["status"], "pending")
        self.assertEqual(events[-1]["machines"][0]["status"], "completed")
        self.assertEqual(events[-1]["machines"][0]["completed"], 1)
        self.assertEqual(events[-1]["machines"][0]["total"], 1)

    def test_current_database_source_skips_download_and_stat(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([record])}, {record["path"]: content})
        database = FakeDatabase()
        database.current[(key[0], key[1], record["path"].casefold())] = (
            record["size"],
            record["last_write_ticks"],
        )

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["totals"]["already_collected"], 1)
        self.assertEqual(summary["totals"]["planned_files"], 0)
        self.assertEqual(transport.fetch_calls, [])
        self.assertEqual(transport.stat_calls, [])
        self.assertEqual(database.ingested, [])

    def test_file_newer_than_fifteen_minutes_is_deferred_without_download(self):
        record, content = self.record(age_minutes=14)
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([record])}, {record["path"]: content})
        database = FakeDatabase()

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["totals"]["unsettled_skipped"], 1)
        self.assertEqual(summary["totals"]["stable"], 0)
        self.assertEqual(summary["failure_class"], "partial")
        self.assertEqual(summary["failure_code"], "source_not_settled")
        self.assertTrue(summary["publishable"])
        self.assertEqual(transport.fetch_calls, [])

    def test_change_after_download_never_reaches_database(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([record])}, {record["path"]: content})
        transport.stat_sequences[record["path"]] = [
            {
                "exists": True,
                "size": record["size"],
                "last_write_ticks": record["last_write_ticks"],
            },
            {
                "exists": True,
                "size": record["size"] + 1,
                "last_write_ticks": record["last_write_ticks"] + 1,
            },
        ]
        database = FakeDatabase()

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["totals"]["downloaded"], 1)
        self.assertEqual(summary["totals"]["ingested"], 0)
        self.assertEqual(summary["totals"]["changed_during_collection"], 1)
        self.assertEqual(summary["failure_class"], "partial")
        self.assertEqual(summary["failure_code"], "source_changed")
        self.assertTrue(summary["publishable"])
        self.assertEqual(database.ingested, [])
        self.assertTrue(
            any(issue["code"] == "source_changed" for issue in database.issues)
        )
        self.assertFalse(any(self.scratch.rglob("*.part")))

    def test_file_download_error_is_recorded_without_aborting_batch(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([record])}, {record["path"]: content})
        transport.fetch_errors.add(record["path"])
        database = FakeDatabase()

        summary = self.collector(database, transport).collect(self.config())

        self.assertEqual(summary["status"], "completed_with_warnings")
        self.assertEqual(summary["totals"]["errors"], 1)
        self.assertEqual(summary["totals"]["ingested"], 0)
        self.assertEqual(summary["failure_class"], "partial")
        self.assertEqual(summary["failure_code"], "remote_partial_failure")
        self.assertTrue(summary["publishable"])
        self.assertEqual(database.finished[-1]["status"], "completed_with_warnings")
        self.assertTrue(
            any(issue["code"] == "remote_file_failed" for issue in database.issues)
        )

    def test_failed_root_does_not_rollback_another_completed_root(self):
        first_record, first_content = self.record()
        machine = dict(self.machine)
        machine["roots"] = [
            dict(self.machine["roots"][0]),
            {"label": "offline", "remote_path": "D:\\offline"},
        ]
        good_key = (machine["id"], machine["roots"][0]["label"])
        bad_key = (machine["id"], machine["roots"][1]["label"])
        transport = FakeTransport(
            {
                good_key: self.inventory([first_record]),
                bad_key: self.inventory([]),
            },
            {first_record["path"]: first_content},
        )
        transport.inventory_errors.add(bad_key)
        database = FakeDatabase()
        events: list[dict] = []
        collector = StartStopCollector(
            database,
            self.scratch,
            transport=transport,
            retry_attempts=1,
            retry_delay_seconds=0,
            reserved_free_bytes=0,
            progress_callback=events.append,
        )

        summary = collector.collect(self.config(machine))

        self.assertEqual(summary["status"], "completed_with_warnings")
        self.assertEqual(summary["totals"]["roots_succeeded"], 1)
        self.assertEqual(summary["totals"]["roots_failed"], 1)
        self.assertEqual(summary["totals"]["ingested"], 1)
        self.assertEqual(summary["totals"]["errors"], 1)
        self.assertEqual(database.ingested[0]["bytes"], first_content)
        self.assertEqual(database.finished[-1]["status"], "completed_with_warnings")
        self.assertEqual(
            events[-1]["machines"][0]["status"],
            "completed_with_warnings",
        )
        percents = [event["percent"] for event in events if event["percent"] is not None]
        self.assertEqual(percents, sorted(percents))

    def test_machine_with_all_roots_failed_remains_failed_in_final_progress(self):
        machine = dict(self.machine)
        machine["roots"] = [
            dict(self.machine["roots"][0]),
            {"label": "offline", "remote_path": "D:\\offline"},
        ]
        first_key = (machine["id"], machine["roots"][0]["label"])
        second_key = (machine["id"], machine["roots"][1]["label"])
        transport = FakeTransport(
            {
                first_key: self.inventory([]),
                second_key: self.inventory([]),
            },
            {},
        )
        transport.inventory_errors.update({first_key, second_key})
        database = FakeDatabase()
        events: list[dict] = []
        collector = StartStopCollector(
            database,
            self.scratch,
            transport=transport,
            retry_attempts=1,
            retry_delay_seconds=0,
            reserved_free_bytes=0,
            progress_callback=events.append,
        )

        summary = collector.collect(self.config(machine))

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["totals"]["roots_succeeded"], 0)
        self.assertEqual(summary["totals"]["roots_failed"], 2)
        self.assertFalse(summary["publishable"])
        self.assertEqual(summary["failure_class"], "fatal")
        self.assertEqual(summary["failure_code"], "no_remote_roots_succeeded")
        self.assertEqual(events[-1]["machines"][0]["status"], "failed")
        self.assertIn("全部 2 个数据源", events[-1]["machines"][0]["message"])

    def test_machine_selector_accepts_id_hostname_or_ip(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        for selector in (self.machine["id"], self.machine["hostname"], self.machine["ip"]):
            with self.subTest(selector=selector):
                transport = FakeTransport(
                    {key: self.inventory([record])}, {record["path"]: content}
                )
                database = FakeDatabase()
                summary = self.collector(database, transport).collect(
                    self.config(), selectors=[selector]
                )
                self.assertEqual(summary["totals"]["ingested"], 1)

    def test_ssh_transport_forces_batch_and_strict_host_key_checking(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        transport = SSHWindowsTransport(known_hosts_file=known_hosts)

        args = transport._ssh_args(machine)

        self.assertIn("BatchMode=yes", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn(f"UserKnownHostsFile={known_hosts}", args)
        self.assertNotIn("StrictHostKeyChecking=no", args)

    def test_missing_ssh_key_is_fatal_and_public_message_hides_path(self):
        missing_key = self.root / "private" / "operator-secret-key"
        machine = dict(self.machine)
        machine["identity_file"] = str(missing_key)

        with self.assertRaises(CredentialConfigurationError) as caught:
            SSHWindowsTransport()._ssh_args(machine)

        self.assertEqual(caught.exception.failure_class, "fatal")
        self.assertEqual(caught.exception.code, "ssh_credentials_invalid")
        self.assertNotIn(str(missing_key), str(caught.exception))

    def test_missing_known_hosts_is_fatal_and_public_message_hides_path(self):
        key_path = self.root / "id_ed25519"
        key_path.write_text("not-used", encoding="utf-8")
        missing_hosts = self.root / "private" / "known-hosts-secret"
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)

        with self.assertRaises(CredentialConfigurationError) as caught:
            SSHWindowsTransport(known_hosts_file=missing_hosts)._ssh_args(machine)

        self.assertEqual(caught.exception.code, "ssh_credentials_invalid")
        self.assertNotIn(str(missing_hosts), str(caught.exception))

    def test_ssh_start_failure_is_fatal_not_remote_warning(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        transport = SSHWindowsTransport(known_hosts_file=known_hosts)

        with mock.patch(
            "echem_platform.start_stop_collection.subprocess.run",
            side_effect=FileNotFoundError("/private/operator/ssh"),
        ):
            with self.assertRaises(LocalProgramError) as caught:
                transport.inventory_root(
                    machine,
                    machine["roots"][0],
                    [".txt"],
                )

        self.assertEqual(caught.exception.code, "local_program_error")
        self.assertNotIn("/private/operator/ssh", str(caught.exception))

    def test_remote_ssh_stderr_is_not_exposed_in_public_error(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        transport = SSHWindowsTransport(known_hosts_file=known_hosts)
        completed = subprocess.CompletedProcess(
            [],
            255,
            stdout=b"",
            stderr=b"Identity file /private/operator/secret-key not accessible",
        )

        with mock.patch(
            "echem_platform.start_stop_collection.subprocess.run",
            return_value=completed,
        ):
            with self.assertRaises(RemoteRootError) as caught:
                transport.inventory_root(
                    machine,
                    machine["roots"][0],
                    [".txt"],
                )

        self.assertEqual(caught.exception.failure_class, "partial")
        self.assertNotIn("secret-key", str(caught.exception))

    def test_database_decision_failure_aborts_instead_of_becoming_root_warning(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )

        class BrokenDatabase(FakeDatabase):
            def needs_download(self, *args, **kwargs):
                raise RuntimeError("sqlite failed at /private/database.sqlite3")

        database = BrokenDatabase()
        with self.assertRaises(LocalDatabaseError) as caught:
            self.collector(database, transport).collect(self.config())

        self.assertEqual(caught.exception.code, "local_database_error")
        self.assertNotIn("/private/database.sqlite3", str(caught.exception))
        self.assertEqual(database.finished[-1]["status"], "failed")
        self.assertFalse(any(issue["code"] == "remote_root_failed" for issue in database.issues))

    def test_database_ingest_failure_aborts_instead_of_becoming_file_warning(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )

        class BrokenDatabase(FakeDatabase):
            def ingest_staged_file(self, *args, **kwargs):
                raise OSError("disk write failed at /private/database.sqlite3")

        database = BrokenDatabase()
        with self.assertRaises(LocalDatabaseError) as caught:
            self.collector(database, transport).collect(self.config())

        self.assertEqual(caught.exception.code, "local_database_error")
        self.assertEqual(database.finished[-1]["status"], "failed")
        self.assertFalse(any(issue["code"] == "remote_file_failed" for issue in database.issues))

    def test_low_local_space_is_fatal_not_file_warning(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )
        database = FakeDatabase()

        with mock.patch(
            "echem_platform.start_stop_collection.shutil.disk_usage",
            return_value=mock.Mock(free=0),
        ):
            with self.assertRaises(LocalStorageError) as caught:
                self.collector(database, transport).collect(self.config())

        self.assertEqual(caught.exception.code, "local_storage_insufficient")
        self.assertEqual(database.finished[-1]["status"], "failed")
        self.assertEqual(database.ingested, [])

    def test_unexpected_transport_bug_is_fatal_not_root_warning(self):
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([])}, {})
        database = FakeDatabase()

        def broken_inventory(*args, **kwargs):
            raise ValueError("program bug at /private/source.py")

        transport.inventory_root = broken_inventory
        with self.assertRaises(UnexpectedCollectionError) as caught:
            self.collector(database, transport).collect(self.config())

        self.assertEqual(caught.exception.code, "unexpected_internal_error")
        self.assertNotIn("/private/source.py", str(caught.exception))
        self.assertEqual(database.finished[-1]["status"], "failed")

    def test_unexpected_file_transport_bug_is_fatal_not_file_warning(self):
        record, content = self.record()
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport(
            {key: self.inventory([record])},
            {record["path"]: content},
        )
        database = FakeDatabase()

        def broken_fetch(*args, **kwargs):
            raise ValueError("program bug at /private/transport.py")

        transport.fetch_file = broken_fetch
        with self.assertRaises(UnexpectedCollectionError) as caught:
            self.collector(database, transport).collect(self.config())

        self.assertEqual(caught.exception.code, "unexpected_internal_error")
        self.assertNotIn("/private/transport.py", str(caught.exception))
        self.assertEqual(database.finished[-1]["status"], "failed")
        self.assertFalse(any(issue["code"] == "remote_file_failed" for issue in database.issues))

    def test_unusable_scratch_path_is_fatal_before_remote_collection(self):
        self.scratch.write_text("not a directory", encoding="utf-8")
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([])}, {})

        with self.assertRaises(LocalFilesystemError) as caught:
            self.collector(FakeDatabase(), transport).collect(self.config())

        self.assertEqual(caught.exception.code, "local_filesystem_error")
        self.assertEqual(transport.inventory_calls, [])

    def test_progress_file_failure_is_fatal_not_silently_ignored(self):
        key = (self.machine["id"], self.machine["roots"][0]["label"])
        transport = FakeTransport({key: self.inventory([])}, {})

        def broken_progress(_payload):
            raise OSError("write failed at /private/progress.json")

        collector = StartStopCollector(
            FakeDatabase(),
            self.scratch,
            transport=transport,
            retry_attempts=1,
            retry_delay_seconds=0,
            reserved_free_bytes=0,
            progress_callback=broken_progress,
        )

        with self.assertRaises(LocalFilesystemError) as caught:
            collector.collect(self.config())

        self.assertEqual(caught.exception.code, "local_filesystem_error")
        self.assertNotIn("/private/progress.json", str(caught.exception))
        self.assertEqual(transport.inventory_calls, [])

    def test_cli_fatal_result_has_stable_code_and_hides_config_path(self):
        missing_config = self.root / "private" / "collection-secret.json"
        result_path = self.root / "result.json"

        with contextlib.redirect_stderr(io.StringIO()):
            returncode = collection_main(
                [
                    "--config",
                    str(missing_config),
                    "--database",
                    str(self.root / "repository.sqlite3"),
                    "--result-json",
                    str(result_path),
                    "--scratch",
                    str(self.scratch),
                ]
            )

        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(returncode, 2)
        self.assertEqual(payload["failure_class"], "fatal")
        self.assertEqual(payload["failure_code"], "collection_config_invalid")
        self.assertFalse(payload["publishable"])
        self.assertNotIn(str(missing_config), json.dumps(payload, ensure_ascii=False))

    def test_connectivity_probe_uses_fixed_read_only_powershell_and_bounded_timeout(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        payload = base64.b64encode(
            json.dumps(
                {"ok": True, "checked_at_utc": "2026-08-03T11:45:00Z"}
            ).encode("utf-8")
        ) + b"\n"
        completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")
        transport = SSHWindowsTransport(known_hosts_file=known_hosts)

        with mock.patch(
            "echem_platform.start_stop_collection.subprocess.run",
            return_value=completed,
        ) as run:
            result = transport.check_connectivity(machine, timeout=7)

        command = run.call_args.args[0]
        self.assertTrue(result["reachable"])
        self.assertEqual(result["checked_at_utc"], "2026-08-03T11:45:00Z")
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ControlMaster=auto", command)
        self.assertIn("ControlPersist=90", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("powershell.exe", command)
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn(str(machine["roots"][0]["remote_path"]), command)

    def test_verified_ssh_download_streams_and_checks_metadata_in_one_session(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        destination = self.root / "download.part"
        content = b"verified-payload"
        ticks = 638898000000000000
        verification = base64.b64encode(
            json.dumps(
                {
                    "before_size": len(content),
                    "before_ticks": ticks,
                    "after_size": len(content),
                    "after_ticks": ticks,
                }
            ).encode("utf-8")
        )

        def run_once(command, **kwargs):
            kwargs["stdout"].write(content)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=None,
                stderr=b"ssh notice\n" + STREAM_VERIFICATION_PREFIX + verification + b"\n",
            )

        transport = SSHWindowsTransport(known_hosts_file=known_hosts)
        with mock.patch(
            "echem_platform.start_stop_collection.subprocess.run",
            side_effect=run_once,
        ) as run:
            result = transport.fetch_file_verified(
                machine,
                self.machine["roots"][0]["remote_path"] + "\\启停.txt",
                destination,
                len(content),
                ticks,
            )

        self.assertEqual(run.call_count, 1)
        self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(result["last_write_ticks"], ticks)

    def test_verified_ssh_download_rejects_changed_source_and_removes_partial(self):
        key_path = self.root / "id_ed25519"
        known_hosts = self.root / "known_hosts"
        key_path.write_text("not-used", encoding="utf-8")
        known_hosts.write_text("host key", encoding="utf-8")
        machine = dict(self.machine)
        machine["identity_file"] = str(key_path)
        destination = self.root / "changed.part"
        content = b"changed-payload"
        ticks = 638898000000000000
        verification = base64.b64encode(
            json.dumps(
                {
                    "before_size": len(content),
                    "before_ticks": ticks,
                    "after_size": len(content),
                    "after_ticks": ticks + 1,
                }
            ).encode("utf-8")
        )

        def run_changed(command, **kwargs):
            kwargs["stdout"].write(content)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=None,
                stderr=STREAM_VERIFICATION_PREFIX + verification + b"\n",
            )

        transport = SSHWindowsTransport(known_hosts_file=known_hosts)
        with mock.patch(
            "echem_platform.start_stop_collection.subprocess.run",
            side_effect=run_changed,
        ):
            with self.assertRaises(SourceChangedError):
                transport.fetch_file_verified(
                    machine,
                    "C:\\data\\启停.txt",
                    destination,
                    len(content),
                    ticks,
                )

        self.assertFalse(destination.exists())

    def test_exp_is_rejected_even_if_added_to_configuration(self):
        config = self.config()
        config["extensions"].append(".exp")
        database = FakeDatabase()
        transport = FakeTransport({}, {})

        with self.assertRaisesRegex(CollectionError, "禁止下载"):
            self.collector(database, transport).collect(config)

        self.assertEqual(transport.inventory_calls, [])
        self.assertEqual(database.ingested, [])


if __name__ == "__main__":
    unittest.main()
