from bridge.bot import EMPTY_RESPONSE_MARKER, _or_fallback


def test_or_fallback_replaces_empty_response_marker():
    assert _or_fallback(EMPTY_RESPONSE_MARKER, "Nothing to compact yet.") == "Nothing to compact yet."


def test_or_fallback_replaces_empty_string():
    assert _or_fallback("", "Cleared.") == "Cleared."


def test_or_fallback_keeps_real_text():
    assert _or_fallback("Compacted (ctrl+o to see full summary)", "Nothing to compact yet.") == (
        "Compacted (ctrl+o to see full summary)"
    )
