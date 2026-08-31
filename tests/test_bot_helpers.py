from bridge.bot import EMPTY_RESPONSE_MARKER, _compact_summary, _mode_markup, _or_fallback


def test_or_fallback_replaces_empty_response_marker():
    assert _or_fallback(EMPTY_RESPONSE_MARKER, "Nothing to compact yet.") == "Nothing to compact yet."


def test_or_fallback_replaces_empty_string():
    assert _or_fallback("", "Cleared.") == "Cleared."


def test_or_fallback_keeps_real_text():
    assert _or_fallback("Compacted (ctrl+o to see full summary)", "Nothing to compact yet.") == (
        "Compacted (ctrl+o to see full summary)"
    )


class _FakeResult:
    def __init__(self, text, duration_ms=None):
        self.text = text
        self.duration_ms = duration_ms


def test_compact_summary_keeps_real_text():
    result = _FakeResult("Compacted (ctrl+o to see full summary)", duration_ms=90000)
    assert _compact_summary(result) == "Compacted (ctrl+o to see full summary)"


def test_compact_summary_reports_nothing_to_do_when_fast_and_empty():
    result = _FakeResult(EMPTY_RESPONSE_MARKER, duration_ms=23)
    assert _compact_summary(result) == "Nothing to compact yet."


def test_compact_summary_reports_silent_success_when_slow_and_empty():
    # This is the real bug this fixes: a compact that genuinely succeeded
    # but printed no chat text (observed live: ~83s, empty stdout) must not
    # be reported as "nothing to compact".
    result = _FakeResult(EMPTY_RESPONSE_MARKER, duration_ms=83000)
    summary = _compact_summary(result)
    assert "83s" in summary
    assert summary != "Nothing to compact yet."


def test_compact_summary_handles_missing_duration():
    result = _FakeResult("", duration_ms=None)
    assert _compact_summary(result) == "Nothing to compact yet."


def test_mode_markup_marks_current_mode():
    markup = _mode_markup("normal")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["✅ normal", "flight"]


def test_mode_markup_switches_check_to_flight():
    markup = _mode_markup("flight")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["normal", "✅ flight"]


def test_mode_markup_callback_data_is_stable_regardless_of_current_mode():
    markup = _mode_markup("flight")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert callbacks == ["mode:normal", "mode:flight"]
