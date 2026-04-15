"""Property-based tests for SLA tracking functionality.

**Validates: Requirements 1.2, 4.1, 4.2, 4.3, 4.5, 5.1, 5.2, 5.4, 5.5, 6.1, 6.2, 6.3, 7.2, 14.1, 14.2, 14.3, 14.4**
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambdas", "configuradora"))

from handler import _formatar_duracao

from hypothesis import given, settings, strategies as st


# ---------------------------------------------------------------------------
# Property 5: Duration formatting is readable and correct
# Feature: sla-tracking, Property 5: duration formatting is readable and correct
# Validates: Requirement 5.5
# ---------------------------------------------------------------------------


def _parse_duracao(s: str) -> int:
    """Parse a formatted duration string back to total minutes.

    Handles formats produced by _formatar_duracao:
        "Xmin"           → X minutes
        "Xh"             → X * 60 minutes
        "XhYmin"         → X * 60 + Y minutes  (no space between h and min parts)
        "Xd"             → X * 1440 minutes
        "Xd Yh"          → X * 1440 + Y * 60 minutes
        "Xd Ymin"        → X * 1440 + Y minutes
        "Xd Yh Zmin"     → X * 1440 + Y * 60 + Z minutes
    """
    import re

    total = 0
    # Extract all (number, unit) pairs from the string
    for num_str, unit in re.findall(r"(\d+)(d|h|min)", s):
        num = int(num_str)
        if unit == "d":
            total += num * 1440
        elif unit == "h":
            total += num * 60
        elif unit == "min":
            total += num
    return total


@settings(max_examples=100)
@given(minutos=st.integers(0, 100000))
def test_property5_duration_formatting(minutos: int) -> None:
    """Property 5: Duration formatting is readable and correct.

    **Validates: Requirement 5.5**

    For any non-negative integer number of minutes, _formatar_duracao SHALL:
    1. Return a non-empty string.
    2. For minutos < 60: result ends with "min" and contains no "h" or "d".
    3. For 60 <= minutos < 1440: result contains "h" and no "d".
    4. For minutos >= 1440: result contains "d".
    5. When parsed back, represents the same number of minutes.
    """
    result = _formatar_duracao(minutos)

    # 1. Result must be a non-empty string
    assert isinstance(result, str), f"Expected str, got {type(result)} for minutos={minutos}"
    assert len(result) > 0, f"Expected non-empty string for minutos={minutos}"

    # 2. For minutos < 60: ends with "min", no "h" or "d"
    if minutos < 60:
        assert result.endswith("min"), (
            f"For minutos={minutos} (<60), expected result ending with 'min', got '{result}'"
        )
        assert "h" not in result, (
            f"For minutos={minutos} (<60), result should not contain 'h', got '{result}'"
        )
        assert "d" not in result, (
            f"For minutos={minutos} (<60), result should not contain 'd', got '{result}'"
        )

    # 3. For 60 <= minutos < 1440: contains "h", no "d"
    elif minutos < 1440:
        assert "h" in result, (
            f"For minutos={minutos} (60-1439), expected result containing 'h', got '{result}'"
        )
        assert "d" not in result, (
            f"For minutos={minutos} (60-1439), result should not contain 'd', got '{result}'"
        )

    # 4. For minutos >= 1440: contains "d"
    else:
        assert "d" in result, (
            f"For minutos={minutos} (>=1440), expected result containing 'd', got '{result}'"
        )

    # 5. Parsed back must represent the same number of minutes
    parsed = _parse_duracao(result)
    assert parsed == minutos, (
        f"Round-trip failed for minutos={minutos}: formatted='{result}', parsed back={parsed}"
    )


from handler import _agrupar_incidentes  # noqa: E402

from datetime import datetime, timezone, timedelta


# ---------------------------------------------------------------------------
# Property 1: Incident grouping by Janela_Consolidacao
# Feature: sla-tracking, Property 1: incident grouping by consolidation window
# Validates: Requirements 4.2, 4.3
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _build_events(gaps_minutes: list[int]) -> list[dict]:
    """Build a list of ERROR/CRITICAL events from a list of gap values.

    The first event starts at _BASE_TS. Each subsequent event is placed
    gap_minutes after the previous one. Severities alternate ERROR/CRITICAL.
    """
    events = []
    current_ts = _BASE_TS
    for i, gap in enumerate(gaps_minutes):
        if i > 0:
            current_ts = current_ts + timedelta(minutes=gap)
        severity = "ERROR" if i % 2 == 0 else "CRITICAL"
        events.append({
            "timestamp": current_ts.isoformat(),
            "severidade": severity,
            "metrica_nome": f"metric_{i}",
        })
    return events


@settings(max_examples=100)
@given(
    gaps=st.lists(
        st.integers(min_value=1, max_value=200),
        min_size=1,
        max_size=10,
    )
)
def test_property1_incident_grouping_by_consolidation_window(
    gaps: list[int],
) -> None:
    """Property 1: Incident grouping by Janela_Consolidacao.

    **Validates: Requirements 4.2, 4.3**

    For any list of ERROR/CRITICAL events:
    - Consecutive events with gap < 60 min SHALL be in the same incident.
    - Consecutive events with gap >= 60 min SHALL be in distinct incidents.
    """
    events = _build_events(gaps)
    incidents = _agrupar_incidentes(events, janela_consolidacao_minutos=60)

    # Reconstruct expected incident boundaries from gaps.
    # gaps[0] is the "starting offset" — the first event is always at _BASE_TS
    # regardless of gaps[0]. Gaps between consecutive events are gaps[1..].
    # We only have inter-event gaps starting from index 1.
    inter_gaps = gaps[1:]  # gap between event i and event i+1

    # Build expected incident groups as lists of event indices
    expected_groups: list[list[int]] = [[0]]
    for idx, gap in enumerate(inter_gaps):
        if gap < 60:
            expected_groups[-1].append(idx + 1)
        else:
            expected_groups.append([idx + 1])

    assert len(incidents) == len(expected_groups), (
        f"Expected {len(expected_groups)} incidents, "
        f"got {len(incidents)}. gaps={gaps}"
    )

    for inc_idx, (incident, group) in enumerate(
        zip(incidents, expected_groups)
    ):
        # Verify incident start matches first event in group
        first_event_ts = datetime.fromisoformat(
            events[group[0]]["timestamp"]
        )
        assert incident["inicio"] == first_event_ts, (
            f"Incident {inc_idx}: expected inicio={first_event_ts}, "
            f"got {incident['inicio']}. gaps={gaps}"
        )

        # Verify incident end matches last event in group
        last_event_ts = datetime.fromisoformat(
            events[group[-1]]["timestamp"]
        )
        assert incident["fim"] == last_event_ts, (
            f"Incident {inc_idx}: expected fim={last_event_ts}, "
            f"got {incident['fim']}. gaps={gaps}"
        )


# ---------------------------------------------------------------------------
# Property 2: Only ERROR/CRITICAL events form incidents
# Feature: sla-tracking, Property 2: only ERROR/CRITICAL form incidents
# Validates: Requirement 4.1
# ---------------------------------------------------------------------------


def _build_mixed_events(
    severity_gap_pairs: list[tuple[str, int]],
) -> list[dict]:
    """Build events from (severity, gap_minutes) pairs.

    The first event starts at _BASE_TS. Each subsequent event is placed
    gap_minutes after the previous one.
    """
    events = []
    current_ts = _BASE_TS
    for i, (severity, gap) in enumerate(severity_gap_pairs):
        if i > 0:
            current_ts = current_ts + timedelta(minutes=gap)
        events.append({
            "timestamp": current_ts.isoformat(),
            "severidade": severity,
            "metrica_nome": f"metric_{i}",
        })
    return events


@settings(max_examples=100)
@given(
    pairs=st.lists(
        st.tuples(
            st.sampled_from(["INFO", "WARNING", "ERROR", "CRITICAL"]),
            st.integers(1, 200),
        ),
        min_size=1,
        max_size=15,
    )
)
def test_property2_only_error_critical_form_incidents(
    pairs: list[tuple[str, int]],
) -> None:
    """Property 2: Only ERROR/CRITICAL events form incidents.

    **Validates: Requirement 4.1**

    For any list of events with mixed severities:
    - All incidents SHALL have severidade_maxima in ("ERROR", "CRITICAL").
    - The total number of incidents SHALL be <= number of ERROR/CRITICAL events.
    """
    events = _build_mixed_events(pairs)
    incidents = _agrupar_incidentes(events)

    error_critical_count = sum(
        1 for sev, _ in pairs if sev in ("ERROR", "CRITICAL")
    )

    # All incidents must have severidade_maxima ERROR or CRITICAL
    for inc in incidents:
        assert inc["severidade_maxima"] in ("ERROR", "CRITICAL"), (
            f"Incident has severidade_maxima={inc['severidade_maxima']!r}, "
            f"expected ERROR or CRITICAL. pairs={pairs}"
        )

    # Number of incidents cannot exceed number of ERROR/CRITICAL events
    assert len(incidents) <= error_critical_count, (
        f"Got {len(incidents)} incidents but only "
        f"{error_critical_count} ERROR/CRITICAL events. pairs={pairs}"
    )

    # If no ERROR/CRITICAL events exist, no incidents should be formed
    if error_critical_count == 0:
        assert len(incidents) == 0, (
            f"Expected 0 incidents when no ERROR/CRITICAL events, "
            f"got {len(incidents)}. pairs={pairs}"
        )


# ---------------------------------------------------------------------------
# Property 3: Max severity follows the hierarchy
# Feature: sla-tracking, Property 3: max severity follows hierarchy
# Validates: Requirement 4.5
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}


@settings(max_examples=100)
@given(
    severities=st.lists(
        st.sampled_from(["INFO", "WARNING", "ERROR", "CRITICAL"]),
        min_size=1,
    )
)
def test_property3_max_severity_follows_hierarchy(
    severities: list[str],
) -> None:
    """Property 3: Max severity follows the hierarchy.

    **Validates: Requirement 4.5**

    For any list of severities:
    - If there are ERROR/CRITICAL events, all incidents SHALL have
      severidade_maxima equal to the highest severity in the list
      according to INFO(0) < WARNING(1) < ERROR(2) < CRITICAL(3).
    - If no ERROR/CRITICAL events exist, no incidents SHALL be formed.
    """
    # Build events with small gaps (< 60 min) so they all end up in one
    # incident when ERROR/CRITICAL events are present.
    pairs = [(sev, 5) for sev in severities]
    events = _build_mixed_events(pairs)
    incidents = _agrupar_incidentes(events)

    has_error_critical = any(
        sev in ("ERROR", "CRITICAL") for sev in severities
    )

    if not has_error_critical:
        assert len(incidents) == 0, (
            f"Expected 0 incidents when no ERROR/CRITICAL events, "
            f"got {len(incidents)}. severities={severities}"
        )
    else:
        # All events have small gaps (5 min < 60 min), so they form
        # exactly one incident.
        assert len(incidents) == 1, (
            f"Expected 1 incident (all gaps < 60 min), "
            f"got {len(incidents)}. severities={severities}"
        )

        expected_max = max(severities, key=lambda s: _SEVERITY_ORDER[s])
        actual_max = incidents[0]["severidade_maxima"]

        assert actual_max == expected_max, (
            f"Expected severidade_maxima={expected_max!r}, "
            f"got {actual_max!r}. severities={severities}"
        )
