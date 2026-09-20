"""Unit tests for W3C CURIE Syntax 1.0 and RFC 3986 reference percent-encoding."""

from reconciliator.utils.curie import (
    encode_curie_reference,
    format_source_uri,
    parse_curie,
    to_curie,
)


def test_encode_curie_reference_disallowed_characters() -> None:
    """Verify curly braces, spaces, controls, and RFC 3986 unwise characters are percent-encoded."""
    # Curly braces { and } must be percent-encoded
    assert encode_curie_reference("foo{bar}baz") == "foo%7Bbar%7Dbaz"

    # RFC 3986 unwise characters: <, >, ", \, ^, `, |
    raw_unwise = 'a<b"c>d\\e^f`g|h'
    encoded = encode_curie_reference(raw_unwise)
    assert "<" not in encoded
    assert ">" not in encoded
    assert '"' not in encoded
    assert "\\" not in encoded
    assert "^" not in encoded
    assert "`" not in encoded
    assert "|" not in encoded

    # Spaces must be encoded
    assert encode_curie_reference("hello world") == "hello%20world"

    # Standard safe URI delimiters must NOT be percent-encoded
    safe_sample = "sub/path:item_123-abc.def~456"
    assert encode_curie_reference(safe_sample) == safe_sample


def test_to_curie_percent_encodes_reference() -> None:
    """Verify to_curie applies percent-encoding to the reference portion."""
    curie = to_curie("melinda", "record_{999}")
    assert curie == "melinda:record_%7B999%7D"
    assert "{" not in curie and "}" not in curie


def test_format_source_uri_template_safety() -> None:
    """Verify format_source_uri does not crash with KeyError/ValueError when identifier has curly braces."""
    # If unescaped, template.format(identifier=clean_id) would raise KeyError or ValueError
    uri = format_source_uri("wikidata", "Q123{45}")
    assert uri == "http://www.wikidata.org/entity/Q123%7B45%7D"
    assert "{" not in uri and "}" not in uri


def test_curie_parse_roundtrip() -> None:
    """Verify parse_curie safely decodes percent-encoded identifiers."""
    curie = to_curie("source", "id{with}braces")
    assert curie == "source:id%7Bwith%7Dbraces"

    parsed_src, parsed_id = parse_curie(curie)
    assert parsed_src == "source"
    assert parsed_id == "id{with}braces"

