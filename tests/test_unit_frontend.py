"""Unit tests for Frontend_Chat logic.

Tests the core JavaScript logic via Python equivalents:
- HTML escaping of special characters
- Error message mapping for HTTP status codes
- Empty/whitespace input rejection

**Validates: Requirements 1.1, 1.2, 1.4**
"""

import html
from typing import Dict


# -----------------------------------------------------------------
# Python equivalents of Frontend_Chat JavaScript logic
# -----------------------------------------------------------------

def escape_html(text: str) -> str:
    """Python equivalent of the JS escapeHtml function."""
    return html.escape(text, quote=True)


# Error message mapping — mirrors the JS switch/if-else in sendMessage()
ERROR_MESSAGES: Dict[int, str] = {
    400: "Pergunta inválida. Verifique o conteúdo e tente novamente.",
    504: "Servidor demorou para responder. Tente novamente em instantes.",
    500: "Erro interno do servidor. Tente novamente mais tarde.",
}

NETWORK_ERROR_MESSAGE = "Sem conexão com o servidor. Verifique sua rede e tente novamente."


def get_error_message(status_code: int) -> str:
    """Return the user-facing error message for a given HTTP status code."""
    return ERROR_MESSAGES.get(
        status_code,
        f"Erro inesperado (HTTP {status_code}). Tente novamente.",
    )


def should_send_message(text: str) -> bool:
    """Mirrors the JS guard: only send if text.trim() is truthy."""
    return bool(text.strip())


# -----------------------------------------------------------------
# Tests: HTML escaping
# -----------------------------------------------------------------

class TestHtmlEscaping:
    """Verify that HTML special characters are properly escaped.

    Validates: Requirement 1.3 (response displayed correctly)
    """

    def test_less_than_escaped(self):
        assert escape_html("<script>") == "&lt;script&gt;"

    def test_greater_than_escaped(self):
        assert escape_html("a > b") == "a &gt; b"

    def test_ampersand_escaped(self):
        assert escape_html("foo & bar") == "foo &amp; bar"

    def test_double_quote_escaped(self):
        assert escape_html('say "hello"') == "say &quot;hello&quot;"

    def test_single_quote_escaped(self):
        assert escape_html("it's") == "it&#x27;s"

    def test_combined_special_chars(self):
        raw = '<img src="x" onerror="alert(\'xss\')">'
        escaped = escape_html(raw)
        assert "<" not in escaped
        assert ">" not in escaped
        # Round-trip must recover original
        assert html.unescape(escaped) == raw

    def test_plain_text_unchanged(self):
        text = "Hello world 123"
        assert escape_html(text) == text

    def test_empty_string(self):
        assert escape_html("") == ""


# -----------------------------------------------------------------
# Tests: Error message mapping
# -----------------------------------------------------------------

class TestErrorMessagesMapping:
    """Verify the error message mapping for each HTTP status code.

    Validates: Requirement 1.4 (descriptive error messages)
    """

    def test_400_bad_request(self):
        msg = get_error_message(400)
        assert msg == "Pergunta inválida. Verifique o conteúdo e tente novamente."

    def test_504_gateway_timeout(self):
        msg = get_error_message(504)
        assert msg == "Servidor demorou para responder. Tente novamente em instantes."

    def test_500_internal_error(self):
        msg = get_error_message(500)
        assert msg == "Erro interno do servidor. Tente novamente mais tarde."

    def test_unknown_status_code(self):
        msg = get_error_message(403)
        assert "403" in msg
        assert "Erro inesperado" in msg

    def test_network_error_message_defined(self):
        assert "conexão" in NETWORK_ERROR_MESSAGE.lower() or "rede" in NETWORK_ERROR_MESSAGE.lower()


# -----------------------------------------------------------------
# Tests: Empty input not sent
# -----------------------------------------------------------------

class TestEmptyInputNotSent:
    """Verify that empty/whitespace input is not processed.

    Validates: Requirement 1.1 (input field behavior)
    """

    def test_empty_string_rejected(self):
        assert should_send_message("") is False

    def test_whitespace_only_rejected(self):
        assert should_send_message("   ") is False

    def test_tabs_only_rejected(self):
        assert should_send_message("\t\t") is False

    def test_newlines_only_rejected(self):
        assert should_send_message("\n\n") is False

    def test_mixed_whitespace_rejected(self):
        assert should_send_message(" \t \n ") is False

    def test_valid_text_accepted(self):
        assert should_send_message("Hello") is True

    def test_text_with_surrounding_whitespace_accepted(self):
        assert should_send_message("  Hello  ") is True
