"""Unit tests for validators and normalizers modules.

Validates: Requirements 5.2, 7.2, 7.3, 10.4, 10.5, 11.1, 11.2, 11.3, 11.4
"""

from lambdas.shared.validators import (
    ValidationResult,
    CrossContaminationResult,
    validate_config_enriquecida,
    validate_evento_estruturado,
    detect_cross_contamination,
    SERVICOS_VALIDOS,
    TIPOS_VALIDOS,
    SEVERIDADES_VALIDAS,
)
from lambdas.shared.normalizers import (
    normalize_medialive_config,
    normalize_mediapackage_config,
    normalize_mediatailor_config,
    normalize_cloudfront_config,
    normalize_cloudwatch_log,
    enrich_evento,
)


# ===================================================================
# Validator tests
# ===================================================================

class TestValidateConfigEnriquecida:
    """Tests for validate_config_enriquecida."""

    def test_valid_record(self):
        record = {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": {"nome_canal": "Canal 1057"},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is True
        assert result.errors == []

    def test_missing_channel_id(self):
        record = {
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("channel_id" in e for e in result.errors)

    def test_empty_channel_id(self):
        record = {
            "channel_id": "   ",
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("vazio" in e for e in result.errors)

    def test_channel_id_wrong_type(self):
        record = {
            "channel_id": 123,
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("string" in e for e in result.errors)

    def test_invalid_servico(self):
        record = {
            "channel_id": "1057",
            "servico": "InvalidService",
            "tipo": "configuracao",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("servico" in e for e in result.errors)

    def test_missing_servico(self):
        record = {
            "channel_id": "1057",
            "tipo": "configuracao",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("servico" in e for e in result.errors)

    def test_invalid_tipo(self):
        record = {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "invalido",
            "dados": {},
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("tipo" in e for e in result.errors)

    def test_missing_dados(self):
        record = {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "configuracao",
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("dados" in e for e in result.errors)

    def test_dados_wrong_type(self):
        record = {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": "not a dict",
        }
        result = validate_config_enriquecida(record)
        assert result.is_valid is False
        assert any("dict" in e for e in result.errors)

    def test_non_dict_record(self):
        result = validate_config_enriquecida("not a dict")
        assert result.is_valid is False
        assert any("dict" in e for e in result.errors)

    def test_multiple_errors(self):
        """Missing all fields should report multiple errors."""
        result = validate_config_enriquecida({})
        assert result.is_valid is False
        assert len(result.errors) == 4  # channel_id, servico, tipo, dados

    def test_all_valid_servicos(self):
        for servico in SERVICOS_VALIDOS:
            record = {
                "channel_id": "test",
                "servico": servico,
                "tipo": "configuracao",
                "dados": {},
            }
            result = validate_config_enriquecida(record)
            assert result.is_valid is True, f"Failed for servico={servico}"

    def test_all_valid_tipos(self):
        for tipo in TIPOS_VALIDOS:
            record = {
                "channel_id": "test",
                "servico": "MediaLive",
                "tipo": tipo,
                "dados": {},
            }
            result = validate_config_enriquecida(record)
            assert result.is_valid is True, f"Failed for tipo={tipo}"


# ===================================================================
# Normalizer tests
# ===================================================================

class TestNormalizeMedialiveConfig:
    """Tests for normalize_medialive_config."""

    def _make_raw_config(self, **overrides):
        base = {
            "Id": "1057",
            "Name": "Canal-1057",
            "State": "RUNNING",
            "Arn": "arn:aws:medialive:us-east-1:123456789:channel:1057",
            "EncoderSettings": {
                "VideoDescriptions": [
                    {
                        "Width": 1920,
                        "Height": 1080,
                        "CodecSettings": {
                            "H264Settings": {
                                "Bitrate": 5000000,
                                "GopSize": 2.0,
                                "GopSizeUnits": "SECONDS",
                                "FramerateNumerator": 30,
                                "FramerateDenominator": 1,
                            }
                        },
                    }
                ],
                "AudioDescriptions": [
                    {"CodecSettings": {"AacSettings": {"Bitrate": 128000}}}
                ],
                "OutputGroups": [
                    {
                        "OutputGroupSettings": {
                            "HlsGroupSettings": {
                                "SegmentLength": 6,
                                "Destination": {
                                    "DestinationRefId": "dest-1"
                                },
                            }
                        }
                    }
                ],
            },
            "InputAttachments": [],
        }
        base.update(overrides)
        return base

    def test_produces_valid_config_enriquecida(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)

        assert result["channel_id"] == "1057"
        assert result["servico"] == "MediaLive"
        assert result["tipo"] == "configuracao"
        assert isinstance(result["dados"], dict)

    def test_extracts_video_codec(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["codec_video"] == "H.264"

    def test_extracts_audio_codec(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["codec_audio"] == "AAC"

    def test_extracts_resolution(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["resolucao"] == "1920x1080"

    def test_extracts_bitrate(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["bitrate_video"] == 5000000

    def test_extracts_gop(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["gop_size"] == 2.0
        assert result["dados"]["gop_unit"] == "SECONDS"

    def test_extracts_framerate(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["framerate"] == 30.0

    def test_extracts_outputs(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert len(result["dados"]["outputs"]) == 1
        assert result["dados"]["outputs"][0]["tipo"] == "HLS"

    def test_stores_complete_config(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        assert result["dados"]["configuracao_completa"] == raw

    def test_minimal_config(self):
        """A minimal config with just an Id should still produce valid output."""
        raw = {"Id": "999"}
        result = normalize_medialive_config(raw)
        assert result["channel_id"] == "999"
        assert result["servico"] == "MediaLive"
        assert result["tipo"] == "configuracao"
        assert isinstance(result["dados"], dict)

    def test_validates_after_normalization(self):
        raw = self._make_raw_config()
        result = normalize_medialive_config(raw)
        validation = validate_config_enriquecida(result)
        assert validation.is_valid is True


class TestNormalizeMediapackageConfig:
    """Tests for normalize_mediapackage_config."""

    def test_produces_valid_config_enriquecida(self):
        raw = {
            "Id": "mp-channel-1",
            "Arn": "arn:aws:mediapackage:us-east-1:123:channels/mp-channel-1",
            "Description": "Test MP Channel",
            "HlsIngest": {
                "IngestEndpoints": [
                    {"Id": "ep1", "Url": "https://ingest.example.com/ep1"}
                ]
            },
            "OriginEndpoints": [
                {
                    "Url": "https://origin.example.com/hls",
                    "HlsPackage": {"SegmentDurationSeconds": 6},
                }
            ],
        }
        result = normalize_mediapackage_config(raw)

        assert result["channel_id"] == "mp-channel-1"
        assert result["servico"] == "MediaPackage"
        assert result["tipo"] == "configuracao"
        assert isinstance(result["dados"], dict)
        assert result["dados"]["outputs"][0]["tipo"] == "HLS"
        assert result["dados"]["outputs"][0]["segment_length"] == 6

    def test_validates_after_normalization(self):
        raw = {"Id": "mp-1", "Arn": "", "OriginEndpoints": []}
        result = normalize_mediapackage_config(raw)
        validation = validate_config_enriquecida(result)
        assert validation.is_valid is True


class TestNormalizeMediaTailorConfig:
    """Tests for normalize_mediatailor_config."""

    def test_produces_valid_config_enriquecida(self):
        raw = {
            "Name": "my-playback-config",
            "PlaybackConfigurationArn": "arn:aws:mediatailor:us-east-1:123:playbackConfiguration/my-playback-config",
            "AdDecisionServerUrl": "https://ads.example.com/vast",
            "CdnConfiguration": {
                "ContentSegmentUrlPrefix": "https://cdn.example.com/segments"
            },
            "SlateAdUrl": "https://slate.example.com/slate.mp4",
            "PersonalizationThresholdSeconds": 2,
            "AvailSuppression": {"Mode": "OFF"},
            "VideoContentSourceUrl": "https://content.example.com",
        }
        result = normalize_mediatailor_config(raw)

        assert result["channel_id"] == "my-playback-config"
        assert result["servico"] == "MediaTailor"
        assert result["tipo"] == "configuracao"
        ad = result["dados"]["ad_insertion"]
        assert ad["ad_decision_server_url"] == "https://ads.example.com/vast"
        assert ad["personalization_threshold_seconds"] == 2

    def test_validates_after_normalization(self):
        raw = {"Name": "test-config"}
        result = normalize_mediatailor_config(raw)
        validation = validate_config_enriquecida(result)
        assert validation.is_valid is True


class TestNormalizeCloudFrontConfig:
    """Tests for normalize_cloudfront_config."""

    def test_produces_valid_config_enriquecida(self):
        raw = {
            "Distribution": {
                "Id": "E1A2B3C4D5",
                "Status": "Deployed",
                "DomainName": "d123.cloudfront.net",
                "DistributionConfig": {
                    "Comment": "Streaming CDN",
                    "Enabled": True,
                    "PriceClass": "PriceClass_100",
                    "Origins": {
                        "Items": [
                            {
                                "Id": "origin-1",
                                "DomainName": "origin.example.com",
                                "OriginPath": "/live",
                            }
                        ]
                    },
                    "DefaultCacheBehavior": {
                        "ViewerProtocolPolicy": "redirect-to-https",
                        "AllowedMethods": {
                            "Items": ["GET", "HEAD"]
                        },
                        "CachePolicyId": "policy-123",
                    },
                },
            }
        }
        result = normalize_cloudfront_config(raw)

        assert result["channel_id"] == "E1A2B3C4D5"
        assert result["servico"] == "CloudFront"
        assert result["tipo"] == "configuracao"
        cdn = result["dados"]["cdn_distribution"]
        assert cdn["domain_name"] == "d123.cloudfront.net"
        assert len(cdn["origins"]) == 1
        assert cdn["origins"][0]["domain_name"] == "origin.example.com"
        assert cdn["enabled"] is True

    def test_validates_after_normalization(self):
        raw = {"Id": "E999", "DistributionConfig": {}}
        result = normalize_cloudfront_config(raw)
        validation = validate_config_enriquecida(result)
        assert validation.is_valid is True


# ===================================================================
# Evento_Estruturado validator tests
# ===================================================================

class TestValidateEventoEstruturado:
    """Tests for validate_evento_estruturado."""

    def _make_valid_evento(self, **overrides):
        base = {
            "timestamp": "2024-01-15T10:30:00Z",
            "canal": "1057",
            "severidade": "ERROR",
            "tipo_erro": "INPUT_LOSS",
            "descricao": "Input signal lost",
        }
        base.update(overrides)
        return base

    def test_valid_record(self):
        result = validate_evento_estruturado(
            self._make_valid_evento()
        )
        assert result.is_valid is True
        assert result.errors == []

    def test_missing_timestamp(self):
        evt = self._make_valid_evento()
        del evt["timestamp"]
        result = validate_evento_estruturado(evt)
        assert result.is_valid is False
        assert any("timestamp" in e for e in result.errors)

    def test_empty_timestamp(self):
        result = validate_evento_estruturado(
            self._make_valid_evento(timestamp="  ")
        )
        assert result.is_valid is False
        assert any("vazio" in e for e in result.errors)

    def test_missing_canal(self):
        evt = self._make_valid_evento()
        del evt["canal"]
        result = validate_evento_estruturado(evt)
        assert result.is_valid is False
        assert any("canal" in e for e in result.errors)

    def test_empty_canal(self):
        result = validate_evento_estruturado(
            self._make_valid_evento(canal="")
        )
        assert result.is_valid is False

    def test_invalid_severidade(self):
        result = validate_evento_estruturado(
            self._make_valid_evento(severidade="DEBUG")
        )
        assert result.is_valid is False
        assert any("severidade" in e for e in result.errors)

    def test_missing_severidade(self):
        evt = self._make_valid_evento()
        del evt["severidade"]
        result = validate_evento_estruturado(evt)
        assert result.is_valid is False

    def test_missing_tipo_erro(self):
        evt = self._make_valid_evento()
        del evt["tipo_erro"]
        result = validate_evento_estruturado(evt)
        assert result.is_valid is False
        assert any("tipo_erro" in e for e in result.errors)

    def test_empty_descricao(self):
        result = validate_evento_estruturado(
            self._make_valid_evento(descricao="   ")
        )
        assert result.is_valid is False

    def test_non_dict_record(self):
        result = validate_evento_estruturado("not a dict")
        assert result.is_valid is False
        assert any("dict" in e for e in result.errors)

    def test_multiple_errors(self):
        result = validate_evento_estruturado({})
        assert result.is_valid is False
        assert len(result.errors) == 5

    def test_all_valid_severidades(self):
        for sev in SEVERIDADES_VALIDAS:
            result = validate_evento_estruturado(
                self._make_valid_evento(severidade=sev)
            )
            assert result.is_valid is True, (
                f"Failed for severidade={sev}"
            )


# ===================================================================
# CloudWatch log normalizer tests
# ===================================================================

class TestNormalizeCloudwatchLog:
    """Tests for normalize_cloudwatch_log."""

    def _make_raw_log(self, **overrides):
        base = {
            "timestamp": 1705312200000,
            "message": "ERROR: input loss detected on channel",
            "logStreamName": "medialive/channel/1057",
            "logGroupName": "/aws/medialive/channels",
        }
        base.update(overrides)
        return base

    def test_produces_evento_estruturado(self):
        raw = self._make_raw_log()
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert "timestamp" in result
        assert "canal" in result
        assert "severidade" in result
        assert "tipo_erro" in result
        assert "descricao" in result
        assert result["servico_origem"] == "MediaLive"

    def test_converts_epoch_ms_to_iso(self):
        raw = self._make_raw_log(timestamp=1705312200000)
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert "2024-01-15" in result["timestamp"]
        assert result["timestamp"].endswith("Z")

    def test_extracts_canal_from_stream(self):
        raw = self._make_raw_log()
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["canal"] == "1057"

    def test_classifies_input_loss(self):
        raw = self._make_raw_log(
            message="ERROR: input loss detected"
        )
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["tipo_erro"] == "INPUT_LOSS"

    def test_classifies_bitrate_drop(self):
        raw = self._make_raw_log(
            message="WARNING: bitrate drop observed"
        )
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["tipo_erro"] == "BITRATE_DROP"

    def test_classifies_severity_from_message(self):
        raw = self._make_raw_log(
            message="CRITICAL: encoder error in pipeline"
        )
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["severidade"] == "CRITICAL"

    def test_unknown_error_classified_as_other(self):
        raw = self._make_raw_log(
            message="INFO: routine check completed"
        )
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["tipo_erro"] == "OTHER"

    def test_preserves_log_group_and_stream(self):
        raw = self._make_raw_log()
        result = normalize_cloudwatch_log(raw, "CloudFront")
        assert result["log_group"] == "/aws/medialive/channels"
        assert result["log_stream"] == (
            "medialive/channel/1057"
        )

    def test_validates_after_normalization(self):
        raw = self._make_raw_log()
        result = normalize_cloudwatch_log(raw, "MediaLive")
        enriched = enrich_evento(result)
        validation = validate_evento_estruturado(enriched)
        assert validation.is_valid is True

    def test_string_timestamp_passthrough(self):
        raw = self._make_raw_log(
            timestamp="2024-01-15T10:30:00Z"
        )
        result = normalize_cloudwatch_log(raw, "MediaLive")
        assert result["timestamp"] == "2024-01-15T10:30:00Z"


# ===================================================================
# Enrichment tests
# ===================================================================

class TestEnrichEvento:
    """Tests for enrich_evento."""

    def _make_evento(self, tipo_erro="INPUT_LOSS"):
        return {
            "timestamp": "2024-01-15T10:30:00Z",
            "canal": "1057",
            "severidade": "ERROR",
            "tipo_erro": tipo_erro,
            "descricao": "Test event",
        }

    def test_enriches_input_loss(self):
        result = enrich_evento(self._make_evento("INPUT_LOSS"))
        assert result["causa_provavel"] == (
            "Perda de sinal de entrada"
        )
        assert result["impacto_estimado"] == "Canal fora do ar"
        assert "conectividade" in result["recomendacao_correcao"]

    def test_enriches_bitrate_drop(self):
        result = enrich_evento(
            self._make_evento("BITRATE_DROP")
        )
        assert "qualidade" in result["causa_provavel"].lower()

    def test_enriches_ad_insertion_failure(self):
        result = enrich_evento(
            self._make_evento("AD_INSERTION_FAILURE")
        )
        assert "anúncio" in result["causa_provavel"].lower()
        assert "MediaTailor" in result["recomendacao_correcao"]

    def test_enriches_cdn_distribution_error(self):
        result = enrich_evento(
            self._make_evento("CDN_DISTRIBUTION_ERROR")
        )
        assert "CDN" in result["causa_provavel"]
        assert "CloudFront" in result["recomendacao_correcao"]

    def test_unknown_tipo_gets_default(self):
        result = enrich_evento(
            self._make_evento("UNKNOWN_TYPE")
        )
        assert result["causa_provavel"] == (
            "Causa não identificada"
        )
        assert result["impacto_estimado"] == (
            "Impacto a ser avaliado"
        )

    def test_preserves_original_fields(self):
        evt = self._make_evento("INPUT_LOSS")
        result = enrich_evento(evt)
        assert result["timestamp"] == evt["timestamp"]
        assert result["canal"] == evt["canal"]
        assert result["severidade"] == evt["severidade"]
        assert result["tipo_erro"] == evt["tipo_erro"]
        assert result["descricao"] == evt["descricao"]

    def test_does_not_mutate_original(self):
        evt = self._make_evento("INPUT_LOSS")
        original_keys = set(evt.keys())
        enrich_evento(evt)
        assert set(evt.keys()) == original_keys

    def test_all_known_tipos_have_enrichment(self):
        known = [
            "INPUT_LOSS", "BITRATE_DROP", "LATENCY_SPIKE",
            "OUTPUT_FAILURE", "ENCODER_ERROR",
            "AD_INSERTION_FAILURE", "CDN_DISTRIBUTION_ERROR",
            "CDN_ORIGIN_ERROR", "CDN_CACHE_ERROR",
        ]
        for tipo in known:
            result = enrich_evento(self._make_evento(tipo))
            assert result["causa_provavel"] != (
                "Causa não identificada"
            ), f"Missing enrichment for {tipo}"


# ===================================================================
# Cross-contamination detection tests
# ===================================================================

class TestDetectCrossContamination:
    """Tests for detect_cross_contamination.

    Validates: Requirements 10.4, 10.5
    """

    def _make_evento(self):
        return {
            "timestamp": "2024-01-15T10:30:00Z",
            "canal": "1057",
            "severidade": "ERROR",
            "tipo_erro": "INPUT_LOSS",
            "descricao": "Input signal lost",
        }

    def _make_config(self):
        return {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "configuracao",
            "dados": {"nome_canal": "Canal 1057"},
        }

    def test_evento_in_kb_config_is_contamination(self):
        result = detect_cross_contamination(
            self._make_evento(), "kb-config"
        )
        assert result.is_contaminated is True
        assert "Evento_Estruturado" in result.alert_message
        assert "kb-config" in result.alert_message

    def test_config_in_kb_logs_is_contamination(self):
        result = detect_cross_contamination(
            self._make_config(), "kb-logs"
        )
        assert result.is_contaminated is True
        assert "Config_Enriquecida" in result.alert_message
        assert "kb-logs" in result.alert_message

    def test_evento_in_kb_logs_is_not_contamination(self):
        result = detect_cross_contamination(
            self._make_evento(), "kb-logs"
        )
        assert result.is_contaminated is False
        assert result.alert_message == ""

    def test_config_in_kb_config_is_not_contamination(self):
        result = detect_cross_contamination(
            self._make_config(), "kb-config"
        )
        assert result.is_contaminated is False
        assert result.alert_message == ""

    def test_non_dict_record_is_not_contamination(self):
        result = detect_cross_contamination(
            "not a dict", "kb-config"
        )
        assert result.is_contaminated is False

    def test_empty_dict_is_not_contamination(self):
        result = detect_cross_contamination({}, "kb-config")
        assert result.is_contaminated is False

    def test_partial_evento_fields_not_contamination(self):
        """Missing one required field should not trigger."""
        record = {
            "timestamp": "2024-01-15T10:30:00Z",
            "canal": "1057",
            "severidade": "ERROR",
            "tipo_erro": "INPUT_LOSS",
            # missing descricao
        }
        result = detect_cross_contamination(
            record, "kb-config"
        )
        assert result.is_contaminated is False

    def test_partial_config_fields_not_contamination(self):
        """Missing one required field should not trigger."""
        record = {
            "channel_id": "1057",
            "servico": "MediaLive",
            "tipo": "configuracao",
            # missing dados
        }
        result = detect_cross_contamination(
            record, "kb-logs"
        )
        assert result.is_contaminated is False

    def test_evento_with_extra_fields_still_detected(self):
        record = self._make_evento()
        record["extra_field"] = "extra"
        result = detect_cross_contamination(
            record, "kb-config"
        )
        assert result.is_contaminated is True

    def test_logs_warning_on_contamination(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            detect_cross_contamination(
                self._make_evento(), "kb-config"
            )
        assert any(
            "Contaminação cruzada" in r.message
            for r in caplog.records
        )
