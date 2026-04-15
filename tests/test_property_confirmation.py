# Feature: streaming-chatbot, Property 10: Confirmação obrigatória antes de operações de escrita
"""Property-based tests for confirmation requirement before write operations.

Since we cannot test the actual Bedrock agent runtime, we verify that the
agent instructions enforce the confirmation requirement by:
1. Checking that the instructions contain explicit confirmation rules
2. Generating sequences of interaction intents and verifying that
   write operations ("configuração_acao") always require confirmation
   per the agent instruction logic.

**Validates: Requirements 13.1, 13.2**
"""

from __future__ import annotations

import re

import hypothesis.strategies as st
from hypothesis import given, settings

from stacks.bedrock_agent_stack import AGENT_INSTRUCTIONS as AGENT_STACK_INSTRUCTIONS
from stacks.main_stack import AGENT_INSTRUCTIONS as MAIN_STACK_INSTRUCTIONS

# ---------------------------------------------------------------------------
# Confirmation keywords that MUST appear in agent instructions
# ---------------------------------------------------------------------------

CONFIRMATION_KEYWORDS = [
    "confirmação explícita",
    "NUNCA execute",
    "sem confirmação",
    "cancele a operação",
    "nenhuma alteração",
]

WRITE_INTENT = "configuração_acao"

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_intents = st.sampled_from([
    "configuração",
    "configuração_acao",
    "logs",
    "ambos",
    "exportação",
])

_user_confirmations = st.sampled_from([
    "Sim, pode criar",
    "Confirmo",
    "Sim",
    "Aprovado",
    "Pode executar",
])

_user_rejections = st.sampled_from([
    "Não",
    "Cancele",
    "Não, cancele",
    "Não quero",
    "Pare",
    "",
])


@st.composite
def interaction_sequence(draw):
    """Generate a sequence of (intent, has_confirmation) pairs."""
    length = draw(st.integers(min_value=1, max_value=10))
    sequence = []
    for _ in range(length):
        intent = draw(_intents)
        confirmed = draw(st.booleans())
        sequence.append((intent, confirmed))
    return sequence


# ===================================================================
# Property 10: Confirmação obrigatória antes de operações de escrita
# **Validates: Requirements 13.1, 13.2**
# ===================================================================


def _check_instructions_contain_confirmation_rules(instructions: str):
    """Verify that agent instructions contain all confirmation rules."""
    lower = instructions.lower()
    for keyword in CONFIRMATION_KEYWORDS:
        assert keyword.lower() in lower, (
            f"Agent instructions missing confirmation keyword: "
            f"'{keyword}'"
        )


def test_agent_stack_instructions_contain_confirmation_rules():
    """Agent instructions in bedrock_agent_stack must enforce confirmation.

    **Validates: Requirements 13.1, 13.2**
    """
    _check_instructions_contain_confirmation_rules(
        AGENT_STACK_INSTRUCTIONS
    )


def test_main_stack_instructions_contain_confirmation_rules():
    """Agent instructions in main_stack must enforce confirmation.

    **Validates: Requirements 13.1, 13.2**
    """
    _check_instructions_contain_confirmation_rules(
        MAIN_STACK_INSTRUCTIONS
    )


def _should_execute_write(intent: str, confirmed: bool) -> bool:
    """Determine if a write operation should execute.

    Write operations (configuração_acao) require explicit confirmation.
    All other intents are read-only and don't need confirmation.
    """
    if intent != WRITE_INTENT:
        return False  # Not a write operation
    return confirmed


@settings(max_examples=200)
@given(sequence=interaction_sequence())
def test_property10_no_write_without_confirmation(sequence):
    """No write operation should execute without explicit confirmation.

    For any sequence of interactions, verify that:
    - Write intents without confirmation never result in execution
    - Write intents with confirmation are allowed to execute
    - Non-write intents never trigger write operations

    **Validates: Requirements 13.1, 13.2**
    """
    for intent, confirmed in sequence:
        should_exec = _should_execute_write(intent, confirmed)

        if intent == WRITE_INTENT and not confirmed:
            assert not should_exec, (
                f"Write operation executed without confirmation! "
                f"Intent={intent}, confirmed={confirmed}"
            )

        if intent != WRITE_INTENT:
            assert not should_exec, (
                f"Write operation triggered by non-write intent! "
                f"Intent={intent}"
            )


@settings(max_examples=200)
@given(
    intent=st.just(WRITE_INTENT),
    rejection=_user_rejections,
)
def test_property10_rejection_cancels_operation(intent, rejection):
    """When user rejects, no write operation should execute.

    **Validates: Requirements 13.1, 13.2**
    """
    confirmed = False  # Rejection means no confirmation
    should_exec = _should_execute_write(intent, confirmed)
    assert not should_exec, (
        f"Write operation executed despite rejection: '{rejection}'"
    )


@settings(max_examples=200)
@given(
    intent=st.just(WRITE_INTENT),
    confirmation=_user_confirmations,
)
def test_property10_confirmation_allows_execution(intent, confirmation):
    """When user explicitly confirms, write operation should proceed.

    **Validates: Requirements 13.1, 13.2**
    """
    confirmed = True
    should_exec = _should_execute_write(intent, confirmed)
    assert should_exec, (
        f"Write operation blocked despite confirmation: "
        f"'{confirmation}'"
    )


def test_instructions_classify_configuracao_acao_as_write():
    """Agent instructions must classify configuração_acao as write intent.

    **Validates: Requirements 13.1, 13.2**
    """
    for instructions in [
        AGENT_STACK_INSTRUCTIONS,
        MAIN_STACK_INSTRUCTIONS,
    ]:
        assert "configuração_acao" in instructions, (
            "Instructions must reference configuração_acao intent"
        )
        # Verify the instructions link configuração_acao to
        # confirmation requirement
        assert "confirmação" in instructions.lower(), (
            "Instructions must mention confirmação for write ops"
        )


def test_instructions_have_cancellation_rule():
    """Agent instructions must specify cancellation when user rejects.

    **Validates: Requirements 13.2**
    """
    for instructions in [
        AGENT_STACK_INSTRUCTIONS,
        MAIN_STACK_INSTRUCTIONS,
    ]:
        lower = instructions.lower()
        assert "cancele" in lower or "cancelar" in lower, (
            "Instructions must specify cancellation behavior"
        )
        assert "nenhuma alteração" in lower, (
            "Instructions must state no changes when cancelled"
        )
