"""Filesystem locations for ShellPilot config, data, state, and cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_state_dir

APP_NAME = "shellpilot"


@dataclass(frozen=True)
class AppPaths:
    """Resolved user-level directories for the app."""

    config_dir: Path
    data_dir: Path
    state_dir: Path
    cache_dir: Path

    @classmethod
    def default(cls) -> AppPaths:
        return cls(
            config_dir=Path(user_config_dir(APP_NAME)),
            data_dir=Path(user_data_dir(APP_NAME)),
            state_dir=Path(user_state_dir(APP_NAME)),
            cache_dir=Path(user_cache_dir(APP_NAME)),
        )

    @property
    def user_config_file(self) -> Path:
        return self.config_dir / "config.toml"


def project_state_dir(workspace: Path) -> Path:
    """Project-local state directory holding plan artifacts and task dirs."""
    return workspace / ".shellpilot"
