"""Regression tests for config-history merges."""

import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from rich.console import Console

from gtnh_updater.state import UpdateState
from gtnh_updater.updater import GTNHUpdater


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )


class ConfigMergeTests(unittest.TestCase):
    def test_existing_history_preserves_user_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "config_repo"
            repo.mkdir()

            write_text(
                repo / "config/GregTech/Pollution.cfg",
                """# Configuration file

pollution {
    B:"Activate Pollution"=true
    I:pollutionVersionSetting=1
}
""",
            )
            write_text(
                repo / "serverutilities/serverutilities.cfg",
                """# Configuration file

world {
    B:chunk_claiming=false
    B:chunk_loading=true
}
""",
            )

            run_git(repo, "init")
            run_git(repo, "config", "user.name", "GTNH Updater Test")
            run_git(repo, "config", "user.email", "updater-test@localhost")
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-m", "New version configs")
            run_git(repo, "branch", "-M", "old-version")

            write_text(
                repo / "config/GregTech/Pollution.cfg",
                """# Configuration file

pollution {
    B:"Activate Pollution"=false
    I:pollutionVersionSetting=1
}
""",
            )
            write_text(
                repo / "serverutilities/serverutilities.cfg",
                """# Configuration file

world {
    B:chunk_claiming=true
    B:chunk_loading=true
}
""",
            )
            run_git(repo, "add", ".")
            run_git(repo, "commit", "-m", "User config changes since last update")

            new_instance = root / "new_instance"
            write_text(
                new_instance / "config/GregTech/Pollution.cfg",
                """# Configuration file

pollution {
    B:"Activate Pollution"=true
    I:pollutionVersionSetting=2
    I:newPollutionSetting=3
}
""",
            )
            write_text(
                new_instance / "serverutilities/serverutilities.cfg",
                """# Configuration file

world {
    B:chunk_claiming=false
    B:chunk_loading=true
    B:newServerUtilitiesSetting=true
}
""",
            )

            updater = GTNHUpdater(Console(file=io.StringIO()))
            state = UpdateState(
                old_instance="",
                new_instance="",
                new_zip="",
                output_dir="",
            )

            updater._add_new_version_and_merge(repo, new_instance, state)

            pollution = (repo / "config/GregTech/Pollution.cfg").read_text()
            self.assertIn('B:"Activate Pollution"=false', pollution)
            self.assertIn("I:pollutionVersionSetting=2", pollution)
            self.assertIn("I:newPollutionSetting=3", pollution)

            serverutilities = (repo / "serverutilities/serverutilities.cfg").read_text()
            self.assertIn("B:chunk_claiming=true", serverutilities)
            self.assertIn("B:chunk_loading=true", serverutilities)
            self.assertIn("B:newServerUtilitiesSetting=true", serverutilities)

            baseline_pollution = run_git(
                repo,
                "show",
                f"{GTNHUpdater.BASELINE_BRANCH}:config/GregTech/Pollution.cfg",
            ).stdout
            self.assertIn('B:"Activate Pollution"=true', baseline_pollution)
            self.assertIn("I:newPollutionSetting=3", baseline_pollution)

            self.assertEqual("", run_git(repo, "status", "--short").stdout)
            self.assertEqual("", run_git(repo, "branch", "--list", GTNHUpdater.NEW_VERSION_BRANCH).stdout)


if __name__ == "__main__":
    unittest.main()
