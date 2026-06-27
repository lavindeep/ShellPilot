"""Tests for write_file, patch_file, and snapshot enforcement (sections 12.4, 12.5, 24.1)."""

import os
from pathlib import Path

from shellpilot.persistence.snapshots import SnapshotStore
from shellpilot.tools.base import ToolContext
from shellpilot.tools.filesystem import READ_FILE
from shellpilot.tools.patch import PATCH_FILE, WRITE_FILE, apply_edit


def ctx(workspace: Path, snapshots: SnapshotStore | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        max_result_tokens=2000,
        snapshots=snapshots if snapshots is not None else SnapshotStore(),
    )


def read_then_ctx(workspace: Path, name: str) -> ToolContext:
    """Read the file through read_file so a snapshot exists, then reuse the context."""
    context = ctx(workspace)
    result = READ_FILE.handler(context, {"path": name})
    assert result.success
    return context


# -- apply_edit unit behavior ---------------------------------------------------


def test_replace_exact() -> None:
    text, error = apply_edit("a = 1\nb = 2\n", "replace_exact", "b = 2", "b = 3")
    assert error == ""
    assert text == "a = 1\nb = 3\n"


def test_insert_before_and_after() -> None:
    text, _ = apply_edit("line2\n", "insert_before", "line2\n", "line1\n")
    assert text == "line1\nline2\n"
    text, _ = apply_edit("line1\n", "insert_after", "line1\n", "line2\n")
    assert text == "line1\nline2\n"


def test_insert_after_anchor_without_trailing_newline() -> None:
    text, error = apply_edit(
        "def greet(name):\n    return name\n",
        "insert_after",
        "def greet(name):",
        '    """doc"""\n',
    )
    assert error == ""
    assert text == 'def greet(name):\n    """doc"""\n    return name\n'


def test_insert_after_anchor_ending_in_newline_does_not_skip_next_line() -> None:
    text, error = apply_edit("a\nb\nc\n", "insert_after", "a\n", "X\n")
    assert error == ""
    assert text == "a\nX\nb\nc\n"


def test_insert_after_last_line_no_trailing_newline() -> None:
    text, error = apply_edit("first\nlast", "insert_after", "last", "appended")
    assert error == ""
    assert text == "first\nlast\nappended"


def test_insert_after_adds_newline_when_new_lacks_one_midfile() -> None:
    text, error = apply_edit("a\nb\n", "insert_after", "a\n", "X")
    assert error == ""
    assert text == "a\nX\nb\n"


def test_insert_after_partial_mid_line_anchor_does_not_split_line() -> None:
    text, error = apply_edit(
        "def greet(name):\n    return name\n",
        "insert_after",
        "def greet",
        '    """doc"""\n',
    )
    assert error == ""
    assert text == 'def greet(name):\n    """doc"""\n    return name\n'


def test_insert_before_mid_line_start_anchor_lands_above_line() -> None:
    text, error = apply_edit(
        "x = 1\ndef greet(name):\n",
        "insert_before",
        "def greet",
        "# comment\n",
    )
    assert error == ""
    assert text == "x = 1\n# comment\ndef greet(name):\n"


def test_insert_before_adds_newline_when_new_lacks_one() -> None:
    text, error = apply_edit("def greet(name):\n", "insert_before", "def greet", "# comment")
    assert error == ""
    assert text == "# comment\ndef greet(name):\n"


def test_insert_after_multiline_anchor_without_trailing_newline() -> None:
    text, error = apply_edit(
        "line1\nline2\nline3\n",
        "insert_after",
        "line1\nline2",
        "inserted\n",
    )
    assert error == ""
    assert text == "line1\nline2\ninserted\nline3\n"


def test_delete_exact() -> None:
    text, _ = apply_edit("keep\ndrop\n", "delete_exact", "drop\n", "")
    assert text == "keep\n"


def test_missing_anchor_rejected() -> None:
    text, error = apply_edit("abc", "replace_exact", "zzz", "y")
    assert text is None
    assert "anchor not found" in error


def test_ambiguous_anchor_rejected() -> None:
    text, error = apply_edit("x\nx\n", "replace_exact", "x\n", "y\n")
    assert text is None
    assert "ambiguous" in error


def test_unknown_operation_rejected() -> None:
    text, error = apply_edit("abc", "swap", "a", "b")
    assert text is None
    assert "unknown operation" in error


# -- snapshot enforcement -------------------------------------------------------


def test_patch_requires_prior_read(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\n")
    result = PATCH_FILE.handler(
        ctx(tmp_path),
        {"path": "f.py", "operation": "replace_exact", "old": "x = 1", "new": "x = 2"},
    )
    assert not result.success
    assert "read it with read_file" in result.content


def test_patch_after_read_succeeds_and_preserves_rest(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a = 1\nb = 2\nc = 3\n")
    context = read_then_ctx(tmp_path, "f.py")
    result = PATCH_FILE.handler(
        context, {"path": "f.py", "operation": "replace_exact", "old": "b = 2", "new": "b = 9"}
    )
    assert result.success
    assert (tmp_path / "f.py").read_text() == "a = 1\nb = 9\nc = 3\n"
    assert "diff" in result.metadata
    assert "-b = 2" in result.metadata["diff"]


def test_diff_header_shows_resolved_relative_path_not_spoof(tmp_path: Path) -> None:
    """The diff header (which becomes the approval panel title) names the
    resolved, workspace-relative target — directory included — for a spoofing
    path, so the displayed file matches the file actually written."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.py").write_text("a = 1\n")
    spoof = "sub/../sub/f.py"  # resolves to sub/f.py
    context = read_then_ctx(tmp_path, spoof)
    result = PATCH_FILE.handler(
        context, {"path": spoof, "operation": "replace_exact", "old": "a = 1", "new": "a = 2"}
    )
    assert result.success
    diff = result.metadata["diff"]
    # Full resolved relative path is shown, not just the basename, and never the
    # raw spoofing string.
    assert "+++ b/sub/f.py" in diff
    assert "+++ b/f.py" not in diff
    assert "sub/../sub" not in diff


def test_write_preview_diff_header_uses_resolved_relative_path(tmp_path: Path) -> None:
    """The write preview shown at the approval gate carries the resolved,
    workspace-relative path in its diff header.

    The target lives in a subdirectory so the resolved relative path
    (``sub/new.txt``) differs from its basename (``new.txt``) — this is what
    makes the test genuinely guard against the old basename-only header.
    """
    from shellpilot.tools.patch import WRITE_FILE

    (tmp_path / "sub").mkdir()
    diff = WRITE_FILE.preview(  # type: ignore[misc]
        ctx(tmp_path),
        {"path": "sub/../sub/new.txt", "content": "hello\n", "mode": "create"},
    )
    assert "+++ b/sub/new.txt" in diff
    assert "+++ b/new.txt" not in diff  # would be the old basename-only header
    assert "sub/../sub/new.txt" not in diff


# -- in-workspace sensitive-file pre-approval previews (design section 15) -------


def _sensitive_ctx(workspace: Path, snapshots: SnapshotStore, mode: str) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        max_result_tokens=2000,
        snapshots=snapshots,
        allow_sensitive_reads=mode,
    )


def test_write_preview_hides_in_workspace_sensitive_before_content(tmp_path: Path) -> None:
    """Overwriting an in-workspace sensitive file (.env) must not render the
    existing secret contents in the pre-approval diff (design section 15)."""
    (tmp_path / ".env").write_text("API_KEY=supersecret123\nDB_PASSWORD=hunter2\n")
    diff = WRITE_FILE.preview(  # type: ignore[misc]
        ctx(tmp_path),  # allow_sensitive_reads defaults to "ask"
        {"path": ".env", "content": "API_KEY=x\n", "mode": "overwrite"},
    )
    assert "supersecret123" not in diff
    assert "hunter2" not in diff
    assert "[sensitive file contents hidden]" in diff


def test_write_preview_shows_non_sensitive_before_content(tmp_path: Path) -> None:
    """A normal in-workspace overwrite still shows its real before-content diff."""
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n")
    diff = WRITE_FILE.preview(  # type: ignore[misc]
        ctx(tmp_path),
        {"path": "notes.txt", "content": "gamma\n", "mode": "overwrite"},
    )
    assert "alpha" in diff
    assert "[sensitive file contents hidden]" not in diff


def test_write_preview_shows_sensitive_when_allow_always(tmp_path: Path) -> None:
    """allow_sensitive_reads='always' opts back into the real before-content."""
    (tmp_path / ".env").write_text("API_KEY=visiblesecret\n")
    diff = WRITE_FILE.preview(  # type: ignore[misc]
        _sensitive_ctx(tmp_path, SnapshotStore(), "always"),
        {"path": ".env", "content": "API_KEY=x\n", "mode": "overwrite"},
    )
    assert "visiblesecret" in diff


def test_write_file_result_diff_hides_sensitive_before(tmp_path: Path) -> None:
    """The diff returned to the model after writing an in-workspace sensitive file
    omits the prior secret contents."""
    (tmp_path / ".env").write_text("TOKEN=abc123secret\n")
    context = read_then_ctx(tmp_path, ".env")
    result = WRITE_FILE.handler(
        context, {"path": ".env", "content": "TOKEN=new\n", "mode": "overwrite"}
    )
    assert result.success
    assert "abc123secret" not in result.content
    assert "abc123secret" not in result.metadata.get("diff", "")
    assert "[sensitive file contents hidden]" in result.content


def test_patch_preview_hides_sensitive_before(tmp_path: Path) -> None:
    """Patching an in-workspace sensitive file does not reveal its contents."""
    (tmp_path / ".env").write_text("SECRET=topsecret\nKEEP=1\n")
    context = read_then_ctx(tmp_path, ".env")
    diff = PATCH_FILE.preview(  # type: ignore[misc]
        context,
        {"path": ".env", "operation": "replace_exact", "old": "KEEP=1", "new": "KEEP=2"},
    )
    assert "topsecret" not in diff
    assert "[sensitive file contents hidden]" in diff


def test_patch_file_result_diff_hides_sensitive(tmp_path: Path) -> None:
    """The patch result diff returned to the model omits the secret contents."""
    (tmp_path / ".env").write_text("SECRET=topsecret\nKEEP=1\n")
    context = read_then_ctx(tmp_path, ".env")
    result = PATCH_FILE.handler(
        context,
        {"path": ".env", "operation": "replace_exact", "old": "KEEP=1", "new": "KEEP=2"},
    )
    assert result.success
    assert "topsecret" not in result.content
    assert "topsecret" not in result.metadata.get("diff", "")
    assert "[sensitive file contents hidden]" in result.content


def test_stale_snapshot_rejected(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\n")
    context = read_then_ctx(tmp_path, "f.py")
    (tmp_path / "f.py").write_text("x = 999  # changed externally\n")
    result = PATCH_FILE.handler(
        context, {"path": "f.py", "operation": "replace_exact", "old": "x = 1", "new": "x = 2"}
    )
    assert not result.success
    assert "changed on disk" in result.content


def test_consecutive_edits_work_without_rereading(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("a = 1\nb = 2\n")
    context = read_then_ctx(tmp_path, "f.py")
    first = PATCH_FILE.handler(
        context, {"path": "f.py", "operation": "replace_exact", "old": "a = 1", "new": "a = 10"}
    )
    assert first.success
    second = PATCH_FILE.handler(
        context, {"path": "f.py", "operation": "replace_exact", "old": "b = 2", "new": "b = 20"}
    )
    assert second.success  # snapshot updated after each successful write


# -- write_file modes -----------------------------------------------------------


def test_create_new_file(tmp_path: Path) -> None:
    result = WRITE_FILE.handler(
        ctx(tmp_path), {"path": "new.txt", "content": "hello\n", "mode": "create"}
    )
    assert result.success
    assert (tmp_path / "new.txt").read_text() == "hello\n"


def test_create_refuses_existing(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old")
    result = WRITE_FILE.handler(ctx(tmp_path), {"path": "f.txt", "content": "new"})
    assert not result.success
    assert "already exists" in result.summary


def test_overwrite_requires_read(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old")
    result = WRITE_FILE.handler(
        ctx(tmp_path), {"path": "f.txt", "content": "new", "mode": "overwrite"}
    )
    assert not result.success


def test_overwrite_after_read(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("old\n")
    context = read_then_ctx(tmp_path, "f.txt")
    result = WRITE_FILE.handler(
        context, {"path": "f.txt", "content": "brand new\n", "mode": "overwrite"}
    )
    assert result.success
    assert (tmp_path / "f.txt").read_text() == "brand new\n"


def test_append_after_read(tmp_path: Path) -> None:
    (tmp_path / "log.txt").write_text("one\n")
    context = read_then_ctx(tmp_path, "log.txt")
    result = WRITE_FILE.handler(context, {"path": "log.txt", "content": "two\n", "mode": "append"})
    assert result.success
    assert (tmp_path / "log.txt").read_text() == "one\ntwo\n"


# -- edge cases (section 24.1) --------------------------------------------------


def test_crlf_preserved(tmp_path: Path) -> None:
    (tmp_path / "win.txt").write_bytes(b"first\r\nsecond\r\n")
    context = read_then_ctx(tmp_path, "win.txt")
    result = PATCH_FILE.handler(
        context,
        {"path": "win.txt", "operation": "replace_exact", "old": "second", "new": "2nd"},
    )
    assert result.success
    assert (tmp_path / "win.txt").read_bytes() == b"first\r\n2nd\r\n"


def test_bom_preserved(tmp_path: Path) -> None:
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbfhello\n")
    context = read_then_ctx(tmp_path, "bom.txt")
    result = PATCH_FILE.handler(
        context,
        {"path": "bom.txt", "operation": "replace_exact", "old": "hello", "new": "world"},
    )
    assert result.success
    assert (tmp_path / "bom.txt").read_bytes() == b"\xef\xbb\xbfworld\n"


def test_executable_bit_preserved(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("echo hi\n")
    script.chmod(0o755)
    context = read_then_ctx(tmp_path, "run.sh")
    result = PATCH_FILE.handler(
        context, {"path": "run.sh", "operation": "replace_exact", "old": "hi", "new": "yo"}
    )
    assert result.success
    assert os.access(script, os.X_OK)


def test_binary_edit_refused(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01data")
    result = PATCH_FILE.handler(
        ctx(tmp_path),
        {"path": "blob.bin", "operation": "replace_exact", "old": "data", "new": "x"},
    )
    assert not result.success
    assert "binary" in result.summary


def test_non_utf8_edit_refused(tmp_path: Path) -> None:
    (tmp_path / "latin.txt").write_bytes("café".encode("latin-1") + b"\n")
    context = ctx(tmp_path)
    READ_FILE.handler(context, {"path": "latin.txt"})  # read records snapshot
    result = PATCH_FILE.handler(
        context,
        {"path": "latin.txt", "operation": "replace_exact", "old": "caf", "new": "bar"},
    )
    assert not result.success
    assert "UTF-8" in result.content
