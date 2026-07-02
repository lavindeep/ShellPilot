from __future__ import annotations

from shellpilot.cli.model_completion import model_completion_matches
from shellpilot.llm.ollama import LocalModel


def _labels(matches: object) -> list[str]:
    return [match.label for match in matches]


def test_model_use_suggestions_filter_installed_models_by_prefix() -> None:
    models = [
        LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000),
        LocalModel(name="gemma4:31b-cloud", size_bytes=31_000_000_000),
        LocalModel(name="llama3:8b", size_bytes=4_000_000_000),
    ]

    matches = model_completion_matches("/model use gem", models)

    assert _labels(matches) == ["gemma4:e4b", "gemma4:31b-cloud"]
    assert matches[0].fill == "/model use gemma4:e4b"


def test_model_use_suggestions_show_size_and_tested_hint() -> None:
    models = [
        LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000),
        LocalModel(name="llama3:8b", size_bytes=4_000_000_000),
    ]

    matches = model_completion_matches("/model use ", models)

    assert matches[0].hint == "4.5 GB"
    assert matches[1].hint == "4.0 GB untested"


def test_model_use_suggestions_escape_spaces_in_fill() -> None:
    matches = model_completion_matches(
        "/model use custom",
        [LocalModel(name="custom model:latest", size_bytes=1_000_000_000)],
    )

    assert _labels(matches) == ["custom model:latest"]
    assert matches[0].fill == "/model use custom\\ model:latest"


def test_non_model_use_command_has_no_model_suggestions() -> None:
    models = [LocalModel(name="gemma4:e4b", size_bytes=4_500_000_000)]

    assert model_completion_matches("/model list", models) == []
    assert model_completion_matches("/cwd set ge", models) == []
