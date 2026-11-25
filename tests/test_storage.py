"""Storage-engine tests: independent byte-level vectors and full backend integration."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from crashcheck.storage.model import materialize, recipes, reductions, _permuted_indices
from crashcheck.storage.trace import TraceWriter, read_trace, safe_file

ROOT = Path(__file__).resolve().parents[1]
try:
    import apsw  # noqa: F401
    HAS_APSW = True
except ImportError:
    HAS_APSW = False


class StorageModelVectors(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "app.sqlite").write_bytes(b"0000")
        self.trace = self.root / "trace"
        self.writer = TraceWriter(self.base, self.trace)

    def finish(self):
        self.writer.finish({"profile": {"journal_mode": "delete"}})

    def output(self, recipe):
        dest = self.root / f"out_{len(list(self.root.glob('out_*')))}"
        dest.mkdir()
        result = materialize(self.base, self.trace, recipe, dest)
        return dest, result

    def test_completed_sync_is_a_per_file_floor(self):
        self.writer.append("begin", ordinal=0)
        self.writer.append("write", "app.sqlite", payload=b"A", offset=0)
        self.writer.append("sync", "app.sqlite", flags=2)
        self.writer.append("write", "app.sqlite", payload=b"B", offset=1)
        self.writer.append("marker", name="at-cut")
        self.finish()

        choices = [r for r in recipes(self.trace, 4) if r["cut"] == 4]
        self.assertEqual(sorted(r["prefixes"]["app.sqlite"] for r in choices), [1, 2])
        kept, event = self.output({"cut": 4, "prefixes": {"app.sqlite": 1}})
        self.assertEqual((kept / "app.sqlite").read_bytes(), b"A000")
        self.assertEqual(event["history"], {"begun": [0], "acknowledged": []})
        full, _ = self.output({"cut": 4, "prefixes": {"app.sqlite": 2}})
        self.assertEqual((full / "app.sqlite").read_bytes(), b"AB00")
        self.assertEqual((self.base / "app.sqlite").read_bytes(), b"0000")
        self.assertEqual([r["prefixes"]["app.sqlite"] for r in
                          reductions(self.trace, {"cut": 4, "prefixes": {"app.sqlite": 2}})], [1])
        with self.assertRaisesRegex(ValueError, "invalid prefix"):
            self.output({"cut": 4, "prefixes": {"app.sqlite": 0}})

    def test_delete_recreate_generation_and_all_kept(self):
        self.writer.append("begin", ordinal=0)
        self.writer.opened("app.sqlite-journal", existed=False, flags=0)
        self.writer.append("write", "app.sqlite-journal", payload=b"old", offset=0)
        self.writer.deleted("app.sqlite-journal", syncdir=True)
        self.writer.opened("app.sqlite-journal", existed=False, flags=0)
        self.writer.append("write", "app.sqlite-journal", payload=b"new", offset=0)
        self.writer.append("ack", ordinal=0)
        self.finish()
        _, events = read_trace(self.trace)
        opens = [e for e in events if e["kind"] == "open"]
        self.assertEqual([e["generation"] for e in opens], [0, 1])
        dest, _ = self.output({"cut": 6, "prefixes": {"app.sqlite-journal": 5}})
        self.assertEqual((dest / "app.sqlite-journal").read_bytes(), b"new")
        with self.assertRaisesRegex(ValueError, "invalid prefix"):
            self.output({"cut": 6, "prefixes": {"app.sqlite-journal": 2}})

    def test_payload_corruption_and_path_traversal_rejected(self):
        event = self.writer.append("write", "app.sqlite", payload=b"x", offset=0)
        self.finish()
        (self.trace / "blobs" / event["blob"]).write_bytes(b"z")
        with self.assertRaisesRegex(ValueError, "corrupted write payload"):
            read_trace(self.trace)
        (self.trace / "blobs" / event["blob"]).write_bytes(b"x")
        with self.assertRaisesRegex(ValueError, "recipe files do not match"):
            self.output({"cut": 0, "prefixes": {"../escape": 1}})
        with self.assertRaisesRegex(ValueError, "unsupported SQLite trace file"):
            safe_file("../escape")

    def test_seeded_prefix_enumeration_is_repeatable_and_complete(self):
        for position in range(4):
            self.writer.append("write", "app.sqlite", payload=bytes([65 + position]),
                               offset=position)
        self.writer.append("marker", name="crash")
        self.finish()
        by_seed = [tuple(r["prefixes"]["app.sqlite"] for r in recipes(self.trace, seed)
                         if r["cut"] == 4 and r["prefixes"]["app.sqlite"] != 4)
                   for seed in range(6)]
        self.assertGreater(len(set(by_seed)), 1)
        self.assertEqual(by_seed[2], tuple(r["prefixes"]["app.sqlite"]
                                           for r in recipes(self.trace, 2)
                                           if r["cut"] == 4 and r["prefixes"]["app.sqlite"] != 4))
        for values in by_seed:
            self.assertEqual(sorted(values), [0, 1, 2, 3])
        self.assertEqual(len(set(_permuted_indices([4, 5, 7], 42, 9))), 140)

    def test_repeated_identical_markers_do_not_expand_search(self):
        self.writer.append("write", "app.sqlite", payload=b"A", offset=0)
        self.writer.append("marker", name="same-state-1")
        self.writer.append("marker", name="same-state-2")
        self.finish()
        generated = list(recipes(self.trace, 0))
        # The final all-kept reference is retained; the redundant second
        # marker itself emits no additional combinations.
        self.assertEqual([r for r in generated if r["cut"] == 2],
                         [{"cut": 2, "prefixes": {"app.sqlite": 1}}])
        self.assertEqual(sorted(r["prefixes"]["app.sqlite"] for r in generated
                                if r["cut"] == 1), [0, 1])


@unittest.skipUnless(HAS_APSW, "APSW is an optional storage-mode dependency")
class RecorderIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / "run"

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "crashcheck", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_record_bookings_and_replay_model_failure(self):
        source = ROOT / "examples" / "booking.py"
        proc = self.command("test", source, "--mode", "modeled", "--model", "file-prefix-v1",
                            "--seed", "42", "--max-candidates", "16", "--out", self.out)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        report = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(report["mode"], "modeled")
        self.assertGreater(report["counts"]["verified-failure"], 0)
        trace_manifest, events = read_trace(self.out / "trace")
        self.assertEqual(trace_manifest["profile"]["synchronous"], "extra")
        self.assertEqual(trace_manifest["profile"]["journal_mode"], "delete")
        self.assertIn("sync", {e["kind"] for e in events})
        self.assertIn("delete", {e["kind"] for e in events})
        failure = next(item for item in report["candidates"]
                       if item.get("outcome", {}).get("status") == "verified-failure")
        self.assertEqual(failure["outcome"]["check_id"], "booking.capacity")
        bundle = self.out / failure["bundle"]
        replay = self.command("replay", bundle, "--regenerate")
        self.assertEqual(replay.returncode, 1, replay.stderr + replay.stdout)
        self.assertTrue(json.loads(replay.stdout)["reproduced"])

    def test_atomic_booking_has_no_modeled_failure(self):
        source = self.root / "atomic_booking.py"
        original = (ROOT / "examples" / "booking.py").read_text()
        source.write_text(original.replace("SPLIT_COMMIT = True", "SPLIT_COMMIT = False"),
                          encoding="utf-8")
        proc = self.command("test", source, "--model", "file-prefix-v1",
                            "--max-candidates", "100", "--out", self.out)
        report = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(report["counts"]["verified-failure"], 0, proc.stdout)
        self.assertIn(proc.returncode, (0, 2))  # 2 means the search budget was truncated.

    def test_wal_profile_is_rejected_explicitly(self):
        source = self.root / "wal_scenario.py"
        original = (ROOT / "examples" / "booking.py").read_text()
        source.write_text(original.replace('CREATE TABLE seats',
                            'PRAGMA journal_mode=WAL; CREATE TABLE seats'), encoding="utf-8")
        proc = self.command("test", source, "--model", "file-prefix-v1", "--out", self.out)
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        self.assertIn("journal_mode=DELETE", proc.stdout)

    def test_scenario_cannot_silently_lower_synchronous(self):
        source = self.root / "unsafe_sync.py"
        original = (ROOT / "examples" / "booking.py").read_text()
        source.write_text(original.replace(
            'db.execute("UPDATE seats SET remaining = remaining - 1")',
            'db.execute("PRAGMA synchronous=OFF")\n        '
            'db.execute("UPDATE seats SET remaining = remaining - 1")', 1), encoding="utf-8")
        proc = self.command("test", source, "--model", "file-prefix-v1", "--out", self.out)
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        self.assertIn("unsupported changed SQLite storage profile", proc.stdout)


if __name__ == "__main__":
    unittest.main()
