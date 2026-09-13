import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from echem_platform.start_stop_resources import ScratchBudget, ScratchSpaceError, estimate_output_bytes, obsolete_snapshot_cache


class ResourceBudgetTests(unittest.TestCase):
    def test_estimate_tracks_real_output_and_growth_not_fixed_400mb(self):
        self.assertEqual(estimate_output_bytes(baseline_output=1_500_000_000, baseline_source=2_000_000_000,
                                             current_source=3_000_000_000, minimum=400_000_000),2_700_000_000)
        self.assertEqual(estimate_output_bytes(baseline_output=0,baseline_source=0,current_source=1,minimum=400_000_000),400_000_000)
        self.assertEqual(estimate_output_bytes(baseline_output=0,baseline_source=100,current_source=200,minimum=0),240)

    def test_analysis_and_export_reservations_cannot_overcommit_or_leak(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget=ScratchBudget(Path(temporary))
            usage=types.SimpleNamespace(free=1000)
            with mock.patch('shutil.disk_usage',return_value=usage):
                with budget.reserve('analysis',700):
                    with self.assertRaises(ScratchSpaceError):
                        with budget.reserve('excel',400):
                            self.fail('overcommit')
                    with budget.reserve('excel-small',200):
                        self.assertEqual(budget.available(),100)
                    self.assertEqual(budget.available(),300)
                self.assertEqual(budget.available(),1000)
                with self.assertRaisesRegex(RuntimeError,'failure'):
                    with budget.reserve('failure',500):
                        raise RuntimeError('failure')
                self.assertEqual(budget.available(),1000)

    def test_resizing_reservation_checks_other_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            budget=ScratchBudget(Path(temporary))
            with mock.patch('shutil.disk_usage',return_value=types.SimpleNamespace(free=1000)):
                with budget.reserve('analysis',300), budget.reserve('export',400):
                    budget.resize('analysis',500)
                    self.assertEqual(budget.available(),100)
                    with self.assertRaises(ScratchSpaceError):
                        budget.resize('analysis',700)
                    self.assertEqual(budget.available(),100)

    def test_only_obsolete_hash_named_cache_is_counted_and_evicted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'cache';root.mkdir()
            old=root/('a'*64);old.mkdir();(old/'rows.txt').write_bytes(b'old')
            current=root/('b'*64);current.mkdir();(current/'rows.txt').write_bytes(b'current')
            unrelated=root/'notes';unrelated.mkdir();(unrelated/'keep.txt').write_bytes(b'keep')
            self.assertEqual(obsolete_snapshot_cache(root,'b'*64),3)
            self.assertTrue(old.exists())
            self.assertEqual(obsolete_snapshot_cache(root,'b'*64,remove=True),3)
            self.assertFalse(old.exists())
            self.assertEqual((current/'rows.txt').read_bytes(),b'current')
            self.assertEqual((unrelated/'keep.txt').read_bytes(),b'keep')

    def test_snapshot_eviction_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)/'cache';root.mkdir()
            external=Path(temporary)/'external';external.mkdir();(external/'keep').write_bytes(b'keep')
            (root/('c'*64)).symlink_to(external,target_is_directory=True)
            with self.assertRaises(ValueError):
                obsolete_snapshot_cache(root,'b'*64,remove=True)
            self.assertEqual((external/'keep').read_bytes(),b'keep')
