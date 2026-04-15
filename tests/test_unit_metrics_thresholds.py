"""Unit tests for specific severity threshold edge cases.

Validates: Requirements 2.4, 2.8, 3.7, 4.5, 4.6, 5.3, 5.5

Tests exact boundary values to ensure correct classification at
threshold edges.
"""

import sys
import os

# Mirror Lambda runtime layout so ``from shared.…`` resolves.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "lambdas",
        "pipeline_logs",
    ),
)

from lambdas.pipeline_logs.handler import classify_severity


# -------------------------------------------------------------------
# MediaTailor — Avail.FillRate thresholds
# -------------------------------------------------------------------


class TestFillRateThresholds:
    """FillRate < 50 → ERROR/FILL_RATE_CRITICO,
    FillRate < 80 → WARNING/FILL_RATE_BAIXO,
    FillRate >= 80 → INFO/METRICAS_NORMAIS.
    """

    def test_fill_rate_80_is_normal(self):
        """FillRate=80 is at the boundary — NOT below 80, so INFO."""
        sev, etype = classify_severity(
            "Avail.FillRate", 80.0, "MediaTailor",
        )
        assert sev == "INFO"
        assert etype == "METRICAS_NORMAIS"

    def test_fill_rate_79_9_is_warning(self):
        """FillRate=79.9 is below 80 → WARNING/FILL_RATE_BAIXO."""
        sev, etype = classify_severity(
            "Avail.FillRate", 79.9, "MediaTailor",
        )
        assert sev == "WARNING"
        assert etype == "FILL_RATE_BAIXO"

    def test_fill_rate_50_is_warning(self):
        """FillRate=50 is NOT below 50, but IS below 80 → WARNING."""
        sev, etype = classify_severity(
            "Avail.FillRate", 50.0, "MediaTailor",
        )
        assert sev == "WARNING"
        assert etype == "FILL_RATE_BAIXO"

    def test_fill_rate_49_9_is_error(self):
        """FillRate=49.9 is below 50 → ERROR/FILL_RATE_CRITICO
        (highest applicable severity)."""
        sev, etype = classify_severity(
            "Avail.FillRate", 49.9, "MediaTailor",
        )
        assert sev == "ERROR"
        assert etype == "FILL_RATE_CRITICO"

    def test_fill_rate_0_is_error(self):
        """FillRate=0 is below 50 → ERROR/FILL_RATE_CRITICO."""
        sev, etype = classify_severity(
            "Avail.FillRate", 0.0, "MediaTailor",
        )
        assert sev == "ERROR"
        assert etype == "FILL_RATE_CRITICO"


# -------------------------------------------------------------------
# MediaLive — PrimaryInputActive thresholds
# -------------------------------------------------------------------


class TestPrimaryInputActiveThresholds:
    """PrimaryInputActive=0 → CRITICAL/FAILOVER_DETECTADO,
    PrimaryInputActive=1 → INFO/METRICAS_NORMAIS.
    """

    def test_primary_input_active_0_is_critical(self):
        """PrimaryInputActive=0 means failover → CRITICAL."""
        sev, etype = classify_severity(
            "PrimaryInputActive", 0, "MediaLive",
        )
        assert sev == "CRITICAL"
        assert etype == "FAILOVER_DETECTADO"

    def test_primary_input_active_1_is_normal(self):
        """PrimaryInputActive=1 means primary active → INFO."""
        sev, etype = classify_severity(
            "PrimaryInputActive", 1, "MediaLive",
        )
        assert sev == "INFO"
        assert etype == "METRICAS_NORMAIS"

    def test_primary_input_active_0_0_is_critical(self):
        """PrimaryInputActive=0.0 (float) → CRITICAL."""
        sev, etype = classify_severity(
            "PrimaryInputActive", 0.0, "MediaLive",
        )
        assert sev == "CRITICAL"
        assert etype == "FAILOVER_DETECTADO"


# -------------------------------------------------------------------
# CloudFront — 5xxErrorRate thresholds
# -------------------------------------------------------------------


class TestCloudFront5xxErrorRateThresholds:
    """5xxErrorRate > 5 → ERROR/CDN_5XX_ALTO,
    5xxErrorRate <= 5 → INFO/METRICAS_NORMAIS.
    """

    def test_5xx_error_rate_5_0_is_normal(self):
        """5xxErrorRate=5.0 is NOT above 5 → INFO."""
        sev, etype = classify_severity(
            "5xxErrorRate", 5.0, "CloudFront",
        )
        assert sev == "INFO"
        assert etype == "METRICAS_NORMAIS"

    def test_5xx_error_rate_5_1_is_error(self):
        """5xxErrorRate=5.1 is above 5 → ERROR/CDN_5XX_ALTO."""
        sev, etype = classify_severity(
            "5xxErrorRate", 5.1, "CloudFront",
        )
        assert sev == "ERROR"
        assert etype == "CDN_5XX_ALTO"

    def test_5xx_error_rate_0_is_normal(self):
        """5xxErrorRate=0 → INFO."""
        sev, etype = classify_severity(
            "5xxErrorRate", 0.0, "CloudFront",
        )
        assert sev == "INFO"
        assert etype == "METRICAS_NORMAIS"


# -------------------------------------------------------------------
# MediaPackage — IngressBytes=0 (INGESTAO_PARADA)
# -------------------------------------------------------------------


class TestIngressBytesThresholds:
    """IngressBytes=0 → ERROR/INGESTAO_PARADA,
    IngressBytes > 0 → INFO/METRICAS_NORMAIS.
    """

    def test_ingress_bytes_0_is_error(self):
        """IngressBytes=0 for a period → ERROR/INGESTAO_PARADA."""
        sev, etype = classify_severity(
            "IngressBytes", 0, "MediaPackage",
        )
        assert sev == "ERROR"
        assert etype == "INGESTAO_PARADA"

    def test_ingress_bytes_positive_is_normal(self):
        """IngressBytes > 0 → INFO/METRICAS_NORMAIS."""
        sev, etype = classify_severity(
            "IngressBytes", 1024.0, "MediaPackage",
        )
        assert sev == "INFO"
        assert etype == "METRICAS_NORMAIS"

    def test_ingress_bytes_0_consecutive_periods(self):
        """IngressBytes=0 for multiple consecutive calls still
        returns ERROR each time (stateless classification)."""
        for _ in range(3):
            sev, etype = classify_severity(
                "IngressBytes", 0, "MediaPackage",
            )
            assert sev == "ERROR"
            assert etype == "INGESTAO_PARADA"
