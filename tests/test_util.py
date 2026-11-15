import tempfile
import unittest
from pathlib import Path

from releaseharbor.errors import SecurityBoundaryError
from releaseharbor.util import (
    atomic_write_text,
    canonical_json,
    relative_posix,
    resolve_under,
    safe_filename,
    safe_segment,
    sha256_bytes,
    sha256_file,
)


class UtilTests(unittest.TestCase):
    def test_safe_filename_accepts_release_names(self):
        self.assertEqual(safe_filename("budget_lines.csv"), "budget_lines.csv")
        self.assertEqual(safe_filename("a.b-c_1.jsonl"), "a.b-c_1.jsonl")

    def test_safe_filename_rejects_traversal_and_reserved_names(self):
        for value in ("../x", "a/b", "a\\b", "..", ".hidden", "CON", "nul.txt", "x:y"):
            with self.subTest(value=value), self.assertRaises(SecurityBoundaryError):
                safe_filename(value)

    def test_safe_segment_rejects_unsafe_ids(self):
        self.assertEqual(safe_segment("2025.1"), "2025.1")
        with self.assertRaises(SecurityBoundaryError):
            safe_segment("../../etc")

    def test_paths_must_stay_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inside = resolve_under(root, "var/file.bin", "file")
            self.assertEqual(inside, (root / "var/file.bin").resolve())
            with self.assertRaises(SecurityBoundaryError):
                resolve_under(root, "../escape.bin", "file")
            with self.assertRaises(SecurityBoundaryError):
                resolve_under(root, ".", "root")

    def test_hash_and_atomic_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reports" / "x.txt"
            atomic_write_text(path, "fixture\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "fixture\n")
            self.assertEqual(sha256_file(path), sha256_bytes(b"fixture\n"))
            self.assertEqual(
                relative_posix(Path(directory), path), "reports/x.txt"
            )

    def test_canonical_json_is_key_stable(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')


if __name__ == "__main__":
    unittest.main()
