# Feature: streaming-chatbot, Property 1: Exibição de resposta no chat
# Feature: streaming-chatbot, Property 2: Preservação do histórico de mensagens
"""Property-based tests for Frontend_Chat logic.

Since the frontend is vanilla HTML/CSS/JS without a browser testing framework,
we test the core logic (HTML escaping, message list management) by implementing
equivalent Python functions that mirror the JavaScript behavior.

**Validates: Requirements 1.3, 1.5**
"""

import html
from typing import List, Tuple

import hypothesis.strategies as st
from hypothesis import given, settings


# -----------------------------------------------------------------
# Python equivalents of Frontend_Chat JavaScript logic
# -----------------------------------------------------------------

def escape_html(text: str) -> str:
    """Python equivalent of the JS escapeHtml function.

    The JS version uses:
        div.textContent = text;
        return div.innerHTML;

    This escapes &, <, >, " and ' — the same chars that Python's
    html.escape(quote=True) handles, plus we also escape single quotes
    to match browser behavior.
    """
    return html.escape(text, quote=True)


def add_message(messages: List[Tuple[str, str]], text: str, msg_type: str) -> List[Tuple[str, str]]:
    """Simulate the addMessage JS function.

    Appends (escaped_text, type) to the message list, mirroring how
    the DOM accumulates chat bubbles.
    """
    escaped = escape_html(text)
    messages.append((escaped, msg_type))
    return messages


# -----------------------------------------------------------------
# Strategies
# -----------------------------------------------------------------

# Broad text strategy: unicode, HTML tags, special chars, long strings
_text_strategy = st.text(
    alphabet=st.characters(
        categories=("L", "M", "N", "P", "S", "Z"),
        include_characters="<>&\"' \t\n\r/\\",
    ),
    min_size=0,
    max_size=500,
)

_message_type = st.sampled_from(["user", "bot", "error"])


# -----------------------------------------------------------------
# Property 1: Exibição de resposta no chat
# -----------------------------------------------------------------

class TestProperty1ExibicaoResposta:
    """Property 1: For any response string, the Frontend_Chat must render
    the complete content in the conversation area, preserving its content
    (no data loss) and escaping HTML tags.

    **Validates: Requirements 1.3**
    """

    @given(text=_text_strategy)
    @settings(max_examples=100)
    def test_escaped_content_preserves_original_data(self, text: str):
        """After HTML escaping, unescaping must recover the original string
        — proving no data is lost during rendering."""
        escaped = escape_html(text)
        recovered = html.unescape(escaped)
        assert recovered == text, (
            f"Data loss detected: original={text!r}, escaped={escaped!r}, "
            f"recovered={recovered!r}"
        )

    @given(text=_text_strategy)
    @settings(max_examples=100)
    def test_html_tags_are_escaped(self, text: str):
        """After escaping, no raw '<' or '>' should remain — all HTML tags
        are neutralised."""
        escaped = escape_html(text)
        assert "<" not in escaped, f"Raw '<' found in escaped output: {escaped!r}"
        assert ">" not in escaped, f"Raw '>' found in escaped output: {escaped!r}"

    @given(text=_text_strategy)
    @settings(max_examples=100)
    def test_ampersand_is_escaped(self, text: str):
        """Bare '&' characters must be escaped to '&amp;' so they don't
        start accidental HTML entities."""
        escaped = escape_html(text)
        # After escaping, every '&' in the output must be part of an entity
        # The simplest check: unescape then re-escape should be stable
        re_escaped = escape_html(html.unescape(escaped))
        assert re_escaped == escaped

    @given(text=st.text(min_size=1, max_size=5000))
    @settings(max_examples=100)
    def test_long_strings_fully_preserved(self, text: str):
        """Even very long strings must be fully preserved after escaping."""
        escaped = escape_html(text)
        recovered = html.unescape(escaped)
        assert recovered == text


# -----------------------------------------------------------------
# Property 2: Preservação do histórico de mensagens
# -----------------------------------------------------------------

_message_pair = st.tuples(
    st.text(min_size=1, max_size=200),
    _message_type,
)


class TestProperty2PreservacaoHistorico:
    """Property 2: For any sequence of messages added to the chat session,
    all previous messages must remain visible in the conversation area
    in the order they were added.

    **Validates: Requirements 1.5**
    """

    @given(messages=st.lists(_message_pair, min_size=1, max_size=50))
    @settings(max_examples=100)
    def test_all_messages_preserved_in_order(self, messages: List[Tuple[str, str]]):
        """Adding N messages must result in exactly N entries, in order."""
        chat_history: List[Tuple[str, str]] = []
        for text, msg_type in messages:
            add_message(chat_history, text, msg_type)

        assert len(chat_history) == len(messages), (
            f"Expected {len(messages)} messages, got {len(chat_history)}"
        )

        for i, ((orig_text, orig_type), (stored_escaped, stored_type)) in enumerate(
            zip(messages, chat_history)
        ):
            assert stored_type == orig_type, (
                f"Message {i}: type mismatch {orig_type!r} vs {stored_type!r}"
            )
            recovered = html.unescape(stored_escaped)
            assert recovered == orig_text, (
                f"Message {i}: content mismatch after unescape"
            )

    @given(messages=st.lists(_message_pair, min_size=2, max_size=50))
    @settings(max_examples=100)
    def test_adding_new_message_does_not_alter_previous(self, messages: List[Tuple[str, str]]):
        """Adding a new message must not change any previously stored message."""
        chat_history: List[Tuple[str, str]] = []

        for idx, (text, msg_type) in enumerate(messages):
            snapshot_before = list(chat_history)
            add_message(chat_history, text, msg_type)

            # All previous entries must be unchanged
            for j in range(idx):
                assert chat_history[j] == snapshot_before[j], (
                    f"Message {j} was altered when adding message {idx}"
                )

    @given(messages=st.lists(_message_pair, min_size=1, max_size=50))
    @settings(max_examples=100)
    def test_message_order_matches_insertion_order(self, messages: List[Tuple[str, str]]):
        """The order of messages in the history must match insertion order."""
        chat_history: List[Tuple[str, str]] = []
        for text, msg_type in messages:
            add_message(chat_history, text, msg_type)

        for i, (orig_text, _) in enumerate(messages):
            recovered = html.unescape(chat_history[i][0])
            assert recovered == orig_text, (
                f"Order violation at index {i}"
            )
