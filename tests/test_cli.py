import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from helpers import free_port, write_project
from releaseharbor.cli import main


class CliTests(unittest.TestCase):
    def test_demo_command_completes_and_strict_mode_reports_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_project(Path(directory), free_port())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["demo", "--config", str(config)]), 0)
            self.assertIn("status: partial", output.getvalue())
            self.assertIn("verified=8", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(["demo", "--config", str(config), "--strict"]), 1
                )

    def test_serve_requires_an_existing_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            config = write_project(Path(directory), free_port())
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                self.assertEqual(main(["serve", "--config", str(config)]), 2)
            self.assertIn("does not exist", error.getvalue())


if __name__ == "__main__":
    unittest.main()
