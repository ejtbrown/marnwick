"""Packaging contracts that can be checked without third-party dependencies."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, "path", [str(ROOT / "packaging"), str(ROOT / "src"), *sys.path]):
    import archive_bundle
    import entry
    from marnwick import worker_launch


class WorkerRoutingTests(unittest.TestCase):
    def test_source_worker_command_keeps_module_invocation(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                worker_launch.worker_command(worker_launch.LAMA_WORKER, "--serve"),
                [sys.executable, "-m", "marnwick.lama_worker", "--serve"],
            )
            self.assertEqual(
                worker_launch.worker_command(worker_launch.GPU_WORKER, "--worker", "CPU"),
                [sys.executable, "-m", "marnwick.gpu_test", "--worker", "CPU"],
            )

    def test_frozen_entry_routes_lama_one_shot_server_and_gpu_before_gui(self):
        cases = (
            (worker_launch.LAMA_WORKER, "marnwick.lama_worker", ["--input", "photo space.png"]),
            (worker_launch.LAMA_WORKER, "marnwick.lama_worker", ["--serve", "--model", "模型.onnx"]),
            (worker_launch.GPU_WORKER, "marnwick.gpu_test", ["--worker", "CPU"]),
        )
        for route, module, args in cases:
            with self.subTest(route=route, args=args):
                with patch.object(sys, "frozen", True, create=True):
                    command = worker_launch.worker_command(route, *args)
                self.assertEqual(command, [sys.executable, route, *args])
                def worker_main():
                    self.assertEqual(sys.argv[1:], args)
                    return 17

                handler = Mock(side_effect=worker_main)
                with (
                    patch.object(sys, "argv", command),
                    patch.object(importlib, "import_module", return_value=Mock(main=handler)) as load,
                ):
                    self.assertEqual(entry.main(), 17)
                load.assert_called_once_with(module)
                handler.assert_called_once_with()


class ProvenanceTests(unittest.TestCase):
    def test_tag_must_match_stable_source_version(self):
        version = archive_bundle.source_version(ROOT)
        self.assertEqual(archive_bundle.version_tag(version), f"v{version}")
        self.assertEqual(archive_bundle.version_tag("1.2.3", "v1.2.3"), "v1.2.3")
        for bad_version, bad_tag in [
            ("1.0.0", "v1.0.1"), ("1.0.0", "1.0.0"), ("1.0.0-rc.1", None),
            ("01.0.0", None), ("1.2.3", "v1.2.3-rc.1"),
        ]:
            with self.subTest(version=bad_version, tag=bad_tag), self.assertRaises(ValueError):
                archive_bundle.version_tag(bad_version, bad_tag)

    def test_receipt_rejects_changed_executable_and_wrong_source(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            dist = root / "dist"
            dist.mkdir()
            bundle = dist / "Marnwick.exe"
            bundle.write_bytes(b"synthetic executable")
            source, target, tag = "a" * 40, "windows-x64", "v1.0.0"
            receipt = {
                "source_sha": source, "target": target, "version_tag": tag,
                "bundle": "dist/Marnwick.exe", "bundle_sha256": archive_bundle.bundle_digest(bundle),
            }
            (dist / "native-build.json").write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual(archive_bundle.verified_bundle(root, source, target, tag), bundle)
            with self.assertRaisesRegex(ValueError, "does not match"):
                archive_bundle.verified_bundle(root, "b" * 40, target, tag)
            with self.assertRaisesRegex(ValueError, "does not match"):
                archive_bundle.verified_bundle(root, source, target, "v1.1.0")
            bundle.write_bytes(b"modified executable")
            with self.assertRaisesRegex(ValueError, "changed after build"):
                archive_bundle.verified_bundle(root, source, target, tag)

    @unittest.skipIf(sys.platform == "win32", "Tests macOS-style POSIX bundle modes and symlinks")
    def test_bundle_digest_includes_executable_mode_and_symlink_target(self):
        with tempfile.TemporaryDirectory() as name:
            bundle = Path(name)
            executable = bundle / "Marnwick"
            executable.write_bytes(b"synthetic executable")
            executable.chmod(0o755)
            link = bundle / "alias"
            link.symlink_to("Marnwick")
            original = archive_bundle.bundle_digest(bundle)
            executable.chmod(0o644)
            self.assertNotEqual(archive_bundle.bundle_digest(bundle), original)
            executable.chmod(0o755)
            self.assertEqual(archive_bundle.bundle_digest(bundle), original)
            link.unlink()
            link.symlink_to("other")
            self.assertNotEqual(archive_bundle.bundle_digest(bundle), original)
