"""Tests for the stdlib HTML-to-text extractor (shellpilot.web.extract)."""

from shellpilot.web.extract import ExtractedPage, extract_text


def test_extracts_paragraph_text() -> None:
    html = "<html><body><p>Hello world</p><p>Second paragraph</p></body></html>"
    page = extract_text(html)
    assert "Hello world" in page.text
    assert "Second paragraph" in page.text
    assert not page.truncated


def test_strips_script_style_and_nav() -> None:
    html = """<html><body>
    <script>alert('x');</script>
    <style>.foo { color: red; }</style>
    <nav><div><a href="#">hidden link</a></div></nav>
    <p>Visible text</p>
    </body></html>"""
    page = extract_text(html)
    assert "Visible text" in page.text
    assert "alert" not in page.text
    assert "color: red" not in page.text
    assert "hidden link" not in page.text


def test_nested_skip_tag_fully_suppressed() -> None:
    html = "<html><body><nav><div><span>deep nav</span></div></nav><p>Main</p></body></html>"
    page = extract_text(html)
    assert "deep nav" not in page.text
    assert "Main" in page.text


def test_title_captured() -> None:
    html = "<html><head><title>My Page</title></head><body><p>Body text</p></body></html>"
    page = extract_text(html)
    assert page.title == "My Page"
    # title text should NOT appear in body text
    assert "My Page" not in page.text


def test_title_defaults_empty_when_missing() -> None:
    html = "<html><body><p>No title here</p></body></html>"
    page = extract_text(html)
    assert page.title == ""


def test_caps_output_and_flags_truncation() -> None:
    long_text = "word " * 10_000  # 50 000 chars
    html = f"<html><body><p>{long_text}</p></body></html>"
    page = extract_text(html, max_chars=100)
    assert len(page.text) <= 100
    assert page.truncated is True


def test_no_truncation_flag_when_within_limit() -> None:
    html = "<html><body><p>short</p></body></html>"
    page = extract_text(html)
    assert page.truncated is False


def test_malformed_html_does_not_raise() -> None:
    garbage = "<<<not>>>html at all</br></p><div unclosed"
    page = extract_text(garbage)
    assert isinstance(page, ExtractedPage)


def test_entities_decoded() -> None:
    html = "<html><body><p>AT&amp;T &lt;rocks&gt; &copy; 2024</p></body></html>"
    page = extract_text(html)
    assert "AT&T" in page.text
    assert "<rocks>" in page.text
    assert "© 2024" in page.text  # copyright symbol


def test_blank_lines_folded() -> None:
    html = "<html><body><p>A</p><p>B</p><p>C</p></body></html>"
    page = extract_text(html)
    # must not have 3+ consecutive blank lines
    import re

    assert not re.search(r"\n{3,}", page.text)


def test_pre_and_list_structure_preserved() -> None:
    html = """<html><body>
    <ul>
      <li>Item one</li>
      <li>Item two</li>
      <li>Item three</li>
    </ul>
    </body></html>"""
    page = extract_text(html)
    lines = [line.strip() for line in page.text.splitlines() if line.strip()]
    assert "Item one" in lines
    assert "Item two" in lines
    assert "Item three" in lines
    # each item should be on its own line
    text = page.text
    assert text.index("Item one") < text.index("Item two") < text.index("Item three")


def test_block_tags_emit_newlines() -> None:
    html = "<html><body><div>First</div><div>Second</div></body></html>"
    page = extract_text(html)
    assert "First" in page.text
    assert "Second" in page.text
    # They should be on separate lines
    assert page.text.index("First") < page.text.index("Second")
    assert "\n" in page.text


def test_multiple_spaces_collapsed() -> None:
    html = "<html><body><p>too    many   spaces</p></body></html>"
    page = extract_text(html)
    assert "too many spaces" in page.text
    assert "  " not in page.text  # no double spaces


def test_header_tags_emit_newlines() -> None:
    html = "<html><body><h1>Title</h1><h2>Subtitle</h2><p>Body</p></body></html>"
    page = extract_text(html)
    assert "Title" in page.text
    assert "Subtitle" in page.text
    assert "Body" in page.text


def test_returns_extracted_page_dataclass() -> None:
    page = extract_text("<html><body><p>hi</p></body></html>")
    assert isinstance(page, ExtractedPage)
    assert isinstance(page.title, str)
    assert isinstance(page.text, str)
    assert isinstance(page.truncated, bool)
