"""Central tool registry (design section 12)."""

from __future__ import annotations

from shellpilot.llm.messages import ToolDefinition
from shellpilot.tools.base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def replace(self, spec: ToolSpec) -> None:
        """Register or overwrite a tool by name (for live settings transitions)."""
        self._specs[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def specs_for_profile(self, profile: str) -> list[ToolSpec]:
        return [spec for spec in self._specs.values() if profile in spec.allowed_profiles]

    def definitions_for_profile(self, profile: str) -> list[ToolDefinition]:
        return [spec.definition for spec in self.specs_for_profile(profile)]


def default_registry() -> ToolRegistry:
    """The full v1 tool surface (design section 12.2)."""
    from shellpilot.tools.command import RUN_COMMAND
    from shellpilot.tools.environment import ENV_INFO
    from shellpilot.tools.filesystem import LIST_DIR, READ_FILE
    from shellpilot.tools.patch import PATCH_FILE, WRITE_FILE
    from shellpilot.tools.search import SEARCH_TEXT

    registry = ToolRegistry()
    for spec in (READ_FILE, LIST_DIR, SEARCH_TEXT, ENV_INFO, RUN_COMMAND, WRITE_FILE, PATCH_FILE):
        registry.register(spec)
    return registry
