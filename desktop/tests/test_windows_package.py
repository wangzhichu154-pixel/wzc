"""Pure archive-contract tests; native Windows execution belongs to CI."""
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from package_windows import PACKAGE, create_archive, pe_machine, sha256


def fake_pe(machine=0x8664):
    data = bytearray(64)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 64)
    return bytes(data) + b"PE\0\0" + struct.pack("<H", machine)


class WindowsPackageTests(unittest.TestCase):
    def test_archive_keeps_complete_runtime_and_verifiable_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            (app / "_internal").mkdir(parents=True)
            (app / "Vector.exe").write_bytes(fake_pe())
            (app / "_internal/Qt6Core.dll").write_bytes(b"test DLL fixture")
            readme = root / "readme.txt"
            readme.write_text("keep the entire folder", encoding="utf-8")
            archive = root / "package.zip"
            manifest = create_archive(app, archive, readme)
            with zipfile.ZipFile(archive) as bundle:
                self.assertIsNone(bundle.testzip())
                self.assertIn(PACKAGE + "/_internal/Qt6Core.dll", bundle.namelist())
                bundle.extractall(root / "extracted")
            for name, digest in manifest.items():
                self.assertEqual(sha256(root / "extracted" / name), digest)

    def test_non_pe_or_truncated_headers_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "Vector.exe"
            for data in (b"not an executable", b"MZ", fake_pe()[:65]):
                exe.write_bytes(data)
                with self.assertRaises(ValueError):
                    pe_machine(exe)

    def test_wrong_architecture_or_missing_runtime_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "app"
            app.mkdir()
            (app / "Vector.exe").write_bytes(fake_pe(0xAA64))
            readme = root / "readme.txt"
            readme.write_text("test", encoding="utf-8")
            with self.assertRaises(ValueError):
                create_archive(app, root / "bundle.zip", readme)
            (app / "_internal").mkdir()
            with self.assertRaises(ValueError):
                create_archive(app, root / "bundle.zip", readme)


if __name__ == "__main__":
    unittest.main()
