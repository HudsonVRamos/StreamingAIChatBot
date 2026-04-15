"""Unit tests for orchestrated channel creation in Lambda_Configuradora.

Validates: Requirements 2.1–2.6, 3.1–3.27, 4.1–4.6, 5.1–5.6, 6.1–6.5, 8.1–8.5
"""

from __future__ import annotations

import json
from unittest.mock import patch

from lambdas.configuradora.handler import (
    handler,
    _build_endpoint_config,
    _execute_orchestrated_creation,
    OrchestrationParams,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(params: dict) -> dict:
    """Build a Bedrock Action Group event for /criarCanalOrquestrado."""
    props = [{"name": k, "value": v} for k, v in params.items()]
    return {
        "apiPath": "/criarCanalOrquestrado",
        "actionGroup": "Action_Group_Config",
        "httpMethod": "POST",
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": props,
                }
            }
        },
    }


def _default_params() -> OrchestrationParams:
    """Return OrchestrationParams with only required fields set."""
    return OrchestrationParams(
        nome_canal="TESTE_KIRO",
        channel_group="VRIO_CHANNELS",
        template_resource_id="warner",
    )


def _response_body(resp: dict) -> dict:
    """Extract the parsed JSON body from a Bedrock response."""
    return json.loads(
        resp["response"]["responseBody"]["application/json"]["body"]
    )


# ---------------------------------------------------------------------------
# 1. Successful orchestration (Req 2.1–2.6, 5.1–5.6, 8.2)
# ---------------------------------------------------------------------------


class TestSuccessfulOrchestration:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.upload_config_json")
    @patch("lambdas.configuradora.handler._create_inputs_for_channel")
    @patch("lambdas.configuradora.handler.get_full_config")
    @patch("lambdas.configuradora.handler.create_resource")
    def test_successful_orchestration(
        self, mock_create, mock_get_cfg, mock_inputs, mock_upload, mock_audit
    ):
        # Step 1: MPV2 channel
        mock_create.side_effect = [
            {
                "resource_id": "TESTE_KIRO",
                "details": {
                    "ChannelName": "TESTE_KIRO",
                    "IngestEndpoints": [
                        {"IngestEndpointUrl": "https://ingest.example.com/v1"}
                    ],
                },
            },
            # Step 2: HLS endpoint
            {"resource_id": "TESTE_KIRO_HLS", "details": {}},
            # Step 2: DASH endpoint
            {"resource_id": "TESTE_KIRO_DASH", "details": {}},
            # Step 4: MediaLive channel
            {"resource_id": "12345678", "details": {"Id": "12345678", "Name": "TESTE_KIRO"}},
        ]

        # Step 3: template config
        mock_get_cfg.return_value = {
            "ChannelClass": "SINGLE_PIPELINE",
            "InputAttachments": [{"InputId": "inp-tpl", "InputSettings": {}}],
            "Name": "TEMPLATE",
            "RoleArn": "arn:aws:iam::role/test",
        }

        # Step 3: inputs
        mock_inputs.return_value = [
            {"InputAttachmentName": "TESTE_KIRO_INPUT_1", "InputId": "inp-1", "InputSettings": {}},
            {"InputAttachmentName": "TESTE_KIRO_INPUT_2", "InputId": "inp-2", "InputSettings": {}},
        ]

        params = _default_params()
        result = _execute_orchestrated_creation(params)

        assert result.success is True
        assert result.recursos_criados["canal_mpv2"] == "TESTE_KIRO"
        assert result.recursos_criados["endpoints"] == [
            "TESTE_KIRO_HLS", "TESTE_KIRO_DASH",
        ]
        assert result.recursos_criados["canal_medialive"] == "12345678"
        assert result.ingest_url == "https://ingest.example.com/v1"
        assert result.canal_medialive_id == "12345678"
        assert result.rollback_executado is False

        # Verify create_resource was called 4 times (MPV2, HLS, DASH, ML)
        assert mock_create.call_count == 4
        mock_upload.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Failure at step 1 — no rollback (Req 2.6)
# ---------------------------------------------------------------------------


class TestFailureStep1NoRollback:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.delete_resource")
    @patch("lambdas.configuradora.handler.create_resource")
    @patch("lambdas.configuradora.handler.get_full_config")
    def test_failure_step1_no_rollback(
        self, mock_get_cfg, mock_create, mock_delete, mock_audit
    ):
        mock_get_cfg.return_value = {
            "Name": "TEMPLATE",
            "ChannelClass": "SINGLE_PIPELINE",
            "Destinations": [],
            "InputAttachments": [],
        }
        mock_create.side_effect = Exception("ConflictException: channel exists")

        params = _default_params()
        result = _execute_orchestrated_creation(params)

        assert result.success is False
        assert result.rollback_executado is True
        assert result.recursos_removidos == []
        assert "ConflictException" in result.erro
        mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Failure at step 2 — rollback MPV2 channel (Req 3.27)
# ---------------------------------------------------------------------------


class TestFailureStep2RollbackMPV2:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.delete_resource")
    @patch("lambdas.configuradora.handler.create_resource")
    @patch("lambdas.configuradora.handler.get_full_config")
    def test_failure_step2_rollback_mpv2(
        self, mock_get_cfg, mock_create, mock_delete, mock_audit
    ):
        mock_get_cfg.return_value = {
            "Name": "TEMPLATE",
            "ChannelClass": "SINGLE_PIPELINE",
            "Destinations": [],
            "InputAttachments": [],
        }
        # Step 1 succeeds, step 2 (HLS endpoint) fails
        mock_create.side_effect = [
            {
                "resource_id": "TESTE_KIRO",
                "details": {
                    "ChannelName": "TESTE_KIRO",
                    "IngestEndpoints": [
                        {"IngestEndpointUrl": "https://ingest.example.com/v1"}
                    ],
                },
            },
            Exception("ServiceException: endpoint creation failed"),
        ]
        mock_delete.return_value = {"status": "deleted", "resource_id": "TESTE_KIRO"}

        params = _default_params()
        result = _execute_orchestrated_creation(params)

        assert result.success is False
        assert result.rollback_executado is True
        assert "TESTE_KIRO" in result.recursos_removidos
        # Only the MPV2 channel should be rolled back
        mock_delete.assert_called_once()
        entry = mock_delete.call_args[0][0]
        assert entry.tipo_recurso == "channel_v2"
        assert entry.resource_id == "TESTE_KIRO"


# ---------------------------------------------------------------------------
# 4. Failure at step 3 — rollback endpoints + MPV2 (Req 4.6)
# ---------------------------------------------------------------------------


class TestFailureStep3RollbackEndpointsAndMPV2:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.delete_resource")
    @patch("lambdas.configuradora.handler._create_inputs_for_channel")
    @patch("lambdas.configuradora.handler.get_full_config")
    @patch("lambdas.configuradora.handler.create_resource")
    def test_failure_step3_rollback_endpoints_and_mpv2(
        self, mock_create, mock_get_cfg, mock_inputs, mock_delete, mock_audit
    ):
        # Steps 1-2 succeed
        mock_create.side_effect = [
            {
                "resource_id": "TESTE_KIRO",
                "details": {
                    "ChannelName": "TESTE_KIRO",
                    "IngestEndpoints": [
                        {"IngestEndpointUrl": "https://ingest.example.com/v1"}
                    ],
                },
            },
            {"resource_id": "TESTE_KIRO_HLS", "details": {}},
            {"resource_id": "TESTE_KIRO_DASH", "details": {}},
        ]

        mock_get_cfg.return_value = {
            "ChannelClass": "SINGLE_PIPELINE",
            "InputAttachments": [{"InputId": "inp-tpl", "InputSettings": {}}],
        }

        # Step 3 fails
        mock_inputs.side_effect = Exception("LimitExceededException: too many inputs")
        mock_delete.return_value = {"status": "deleted", "resource_id": "ok"}

        params = _default_params()
        result = _execute_orchestrated_creation(params)

        assert result.success is False
        assert result.rollback_executado is True
        # Should rollback: DASH endpoint, HLS endpoint, MPV2 channel (reverse order)
        assert mock_delete.call_count == 3
        rolled_back_types = [c[0][0].tipo_recurso for c in mock_delete.call_args_list]
        assert rolled_back_types == ["origin_endpoint_v2", "origin_endpoint_v2", "channel_v2"]


# ---------------------------------------------------------------------------
# 5. Failure at step 4 — complete rollback (Req 5.6, 6.1)
# ---------------------------------------------------------------------------


class TestFailureStep4CompleteRollback:
    @patch("lambdas.configuradora.handler.store_audit_log")
    @patch("lambdas.configuradora.handler.delete_resource")
    @patch("lambdas.configuradora.handler._create_inputs_for_channel")
    @patch("lambdas.configuradora.handler.get_full_config")
    @patch("lambdas.configuradora.handler.create_resource")
    def test_failure_step4_complete_rollback(
        self, mock_create, mock_get_cfg, mock_inputs, mock_delete, mock_audit
    ):
        # Steps 1-3 succeed, step 4 fails
        mock_create.side_effect = [
            {
                "resource_id": "TESTE_KIRO",
                "details": {
                    "ChannelName": "TESTE_KIRO",
                    "IngestEndpoints": [
                        {"IngestEndpointUrl": "https://ingest.example.com/v1"}
                    ],
                },
            },
            {"resource_id": "TESTE_KIRO_HLS", "details": {}},
            {"resource_id": "TESTE_KIRO_DASH", "details": {}},
            # Step 4: MediaLive channel creation fails
            Exception("UnprocessableEntityException: invalid config"),
        ]

        mock_get_cfg.return_value = {
            "ChannelClass": "SINGLE_PIPELINE",
            "InputAttachments": [{"InputId": "inp-tpl", "InputSettings": {}}],
            "Name": "TEMPLATE",
            "RoleArn": "arn:aws:iam::role/test",
        }

        mock_inputs.return_value = [
            {"InputAttachmentName": "TESTE_KIRO_INPUT_1", "InputId": "inp-1", "InputSettings": {}},
            {"InputAttachmentName": "TESTE_KIRO_INPUT_2", "InputId": "inp-2", "InputSettings": {}},
        ]

        mock_delete.return_value = {"status": "deleted", "resource_id": "ok"}

        params = _default_params()
        result = _execute_orchestrated_creation(params)

        assert result.success is False
        assert result.rollback_executado is True
        # Should rollback: 2 inputs + 2 endpoints + 1 MPV2 channel = 5 resources
        assert mock_delete.call_count == 5
        rolled_back_types = [c[0][0].tipo_recurso for c in mock_delete.call_args_list]
        # Reverse order: inputs (last created first), then endpoints, then channel
        assert rolled_back_types == [
            "input", "input",
            "origin_endpoint_v2", "origin_endpoint_v2",
            "channel_v2",
        ]


# ---------------------------------------------------------------------------
# 6. Handler: missing params returns 400 (Req 8.4)
# ---------------------------------------------------------------------------


class TestHandlerMissingParams:
    def test_handler_missing_params_returns_400(self):
        event = _make_event({
            "nome_canal": "TESTE",
            # missing channel_group and template_resource_id
        })
        resp = handler(event, None)
        body = _response_body(resp)

        assert resp["response"]["httpStatusCode"] == 400
        assert "parametros_faltantes" in body
        assert "channel_group" in body["parametros_faltantes"]
        assert "template_resource_id" in body["parametros_faltantes"]

    def test_handler_all_params_missing_returns_400(self):
        event = _make_event({})
        resp = handler(event, None)
        body = _response_body(resp)

        assert resp["response"]["httpStatusCode"] == 400
        assert "parametros_faltantes" in body
        assert len(body["parametros_faltantes"]) == 3
        assert "nome_canal" in body["parametros_faltantes"]
        assert "channel_group" in body["parametros_faltantes"]
        assert "template_resource_id" in body["parametros_faltantes"]


# ---------------------------------------------------------------------------
# 7. Default values for optional parameters (Req 8.3)
# ---------------------------------------------------------------------------


class TestDefaultValues:
    def test_default_values_applied(self):
        params = OrchestrationParams(
            nome_canal="CANAL_X",
            channel_group="GRP",
            template_resource_id="tpl",
        )
        assert params.segment_duration == 6
        assert params.drm_resource_id == ""
        assert params.manifest_window_seconds == 7200
        assert params.startover_window_hls_seconds == 900
        assert params.startover_window_dash_seconds == 14460
        assert params.ts_include_dvb_subtitles is True
        assert params.min_buffer_time_seconds == 2
        assert params.suggested_presentation_delay_seconds == 12


# ---------------------------------------------------------------------------
# 8. DRM config HLS — CBCS / FAIRPLAY (Req 3.9)
# ---------------------------------------------------------------------------


class TestDrmConfigHLS:
    def test_drm_config_hls_cbcs_fairplay(self):
        params = _default_params()
        config = _build_endpoint_config(params, "HLS")

        encryption = config["Segment"]["Encryption"]
        assert encryption["EncryptionMethod"]["CmafEncryptionMethod"] == "CBCS"
        assert encryption["SpekeKeyProvider"]["DrmSystems"] == ["FAIRPLAY"]


# ---------------------------------------------------------------------------
# 9. DRM config DASH — CENC / PLAYREADY+WIDEVINE (Req 3.10)
# ---------------------------------------------------------------------------


class TestDrmConfigDASH:
    def test_drm_config_dash_cenc_playready_widevine(self):
        params = _default_params()
        config = _build_endpoint_config(params, "DASH")

        encryption = config["Segment"]["Encryption"]
        assert encryption["EncryptionMethod"]["CmafEncryptionMethod"] == "CENC"
        assert encryption["SpekeKeyProvider"]["DrmSystems"] == ["PLAYREADY", "WIDEVINE"]


# ---------------------------------------------------------------------------
# 10. Fixed fields — HLS (Req 3.3, 3.5, 3.14)
# ---------------------------------------------------------------------------


class TestFixedFieldsHLS:
    def test_fixed_fields_hls(self):
        params = _default_params()
        config = _build_endpoint_config(params, "HLS")

        assert config["ContainerType"] == "CMAF"
        assert config["Segment"]["SegmentName"] == "segment"
        assert config["HlsManifests"][0]["ManifestName"] == "master"
        assert config["Segment"]["TsUseAudioRenditionGroup"] is True
        assert config["Segment"]["IncludeIframeOnlyStreams"] is False


# ---------------------------------------------------------------------------
# 11. Fixed fields — DASH (Req 3.3, 3.5, 3.17, 3.21–3.24)
# ---------------------------------------------------------------------------


class TestFixedFieldsDASH:
    def test_fixed_fields_dash(self):
        params = _default_params()
        config = _build_endpoint_config(params, "DASH")

        assert config["ContainerType"] == "CMAF"
        assert config["Segment"]["SegmentName"] == "segment"
        assert config["DashManifests"][0]["ManifestName"] == "manifest"
        assert config["Segment"]["TsUseAudioRenditionGroup"] is True
        assert config["Segment"]["IncludeIframeOnlyStreams"] is False
        dash = config["DashManifests"][0]
        assert dash["SegmentTemplateFormat"] == "NUMBER_WITH_TIMELINE"
        assert dash["PeriodTriggers"] == [
            "AVAILS", "DRM_KEY_ROTATION", "SOURCE_CHANGES", "SOURCE_DISRUPTIONS"
        ]
        assert dash["DrmSignaling"] == "INDIVIDUAL"
        assert dash["UtcTiming"] == {"TimingMode": "UTC_DIRECT"}
        assert dash["MinUpdatePeriodSeconds"] == params.segment_duration
