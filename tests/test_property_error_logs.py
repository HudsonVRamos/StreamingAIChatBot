# Feature: streaming-chatbot, Property 6: Logs de erro contêm informações de identificação
"""Property-based tests for error log identification.

**Validates: Requirements 5.4, 7.5**

For any failure in configuration extraction or log collection,
the structured error log must contain the source identifier
(channel_id, distribution_id, or configuration_name) and the
reason for the failure.
"""

import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import given, settings

# Mock boto3 before importing the handler module so that
# module-level boto3 usage does not fail.
sys.modules.setdefault("boto3", MagicMock())

from lambdas.pipeline_config.handler import _record_error  # noqa: E402


# -------------------------------------------------------------------
# Strategies
# -------------------------------------------------------------------

_SERVICES = ["MediaLive", "MediaPackage", "MediaTailor", "CloudFront"]

_service_strategy = st.sampled_from(_SERVICES)

_resource_id_strategy = st.text(
    min_size=1,
    max_size=80,
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S"),
        blacklist_characters="\x00",
    ),
)

_error_reason_strategy = st.text(
    min_size=1,
    max_size=200,
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S", "Z"),
        blacklist_characters="\x00",
    ),
)


# -------------------------------------------------------------------
# Property test
# -------------------------------------------------------------------


@settings(max_examples=100)
@given(
    service=_service_strategy,
    resource_id=_resource_id_strategy,
    reason=_error_reason_strategy,
)
def test_error_log_contains_identification(service, resource_id, reason):
    """Error entries recorded by _record_error contain service, resource_id and reason.

    Steps:
    1. Build a fresh results dict.
    2. Call _record_error with generated service, resource_id and reason.
    3. Assert the error entry contains all three identifiers.
    4. Assert logger.error was called with a message containing
       the resource_id and reason.
    """
    results = {"errors": []}

    with patch(
        "lambdas.pipeline_config.handler.logger"
    ) as mock_logger:
        _record_error(results, service, resource_id, reason)

    # -- Verify the error entry in results --
    assert len(results["errors"]) == 1
    entry = results["errors"][0]

    assert entry["service"] == service, (
        f"Expected service={service!r}, got {entry.get('service')!r}"
    )
    assert entry["resource_id"] == resource_id, (
        f"Expected resource_id={resource_id!r}, "
        f"got {entry.get('resource_id')!r}"
    )
    assert entry["reason"] == reason, (
        f"Expected reason={reason!r}, got {entry.get('reason')!r}"
    )

    # -- Verify logger.error was called with identifying info --
    mock_logger.error.assert_called_once()
    log_args = mock_logger.error.call_args
    # Build the formatted log message from positional args
    log_message = log_args[0][0] % log_args[0][1:]

    assert resource_id in log_message, (
        f"resource_id {resource_id!r} not found in log message: "
        f"{log_message!r}"
    )
    assert reason in log_message, (
        f"reason {reason!r} not found in log message: "
        f"{log_message!r}"
    )
