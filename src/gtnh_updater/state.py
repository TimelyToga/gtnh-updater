"""State persistence for resumable updates."""

import json
import os
import platform
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class UpdateState:
    """Tracks the state of an in-progress update for resume functionality."""

    old_instance: str
    new_instance: str
    new_zip: str
    output_dir: str
    stage: str = "started"
    config_repo_path: Optional[str] = None
    conflicts: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    @classmethod
    def get_state_dir(cls) -> Path:
        """Get the platform-specific state directory."""
        system = platform.system().lower()

        if system == "windows":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        elif system == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))

        state_dir = base / "gtnh-updater"
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    def save(self) -> Path:
        """Save state to a file and return the path."""
        state_dir = self.get_state_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        state_file = state_dir / f"update_state_{timestamp}.json"

        with open(state_file, "w") as f:
            json.dump(asdict(self), f, indent=2)

        # Also save as "latest" for easy resumption
        latest_file = state_dir / "latest_state.json"
        with open(latest_file, "w") as f:
            json.dump(asdict(self), f, indent=2)

        return state_file

    @classmethod
    def load(cls, state_file: Path) -> "UpdateState":
        """Load state from a file."""
        with open(state_file) as f:
            data = json.load(f)
        return cls(**data)

    @classmethod
    def find_latest_state_file(cls) -> Optional[Path]:
        """Find the most recent state file."""
        state_dir = cls.get_state_dir()
        latest_file = state_dir / "latest_state.json"

        if latest_file.exists():
            return latest_file

        # Fall back to looking for timestamped files
        state_files = list(state_dir.glob("update_state_*.json"))
        if not state_files:
            return None

        return max(state_files, key=lambda p: p.stat().st_mtime)

    def clear(self) -> None:
        """Clear the state files after successful completion."""
        state_dir = self.get_state_dir()
        latest_file = state_dir / "latest_state.json"

        if latest_file.exists():
            latest_file.unlink()
