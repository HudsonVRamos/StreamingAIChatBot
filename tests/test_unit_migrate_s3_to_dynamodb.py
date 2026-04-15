"""Unit tests for scripts/migrate_s3_to_dynamodb.py."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from scripts.migrate_s3_to_dynamodb import (
    _is_within_last_n_days,
    parse_args,
    transform_config,
    transform_log,
    migrate_configs,
    migrate_logs,
)


# ── transform_config ──────────────────────────────────────────

class TestTransformConfig:
    def test_basic_config(self):
        config = {
            "servico": "MediaLive",
            "tipo": "channel",
            "channel_id": "1234567",
            "nome_canal": "0001_WARNER",
            "estado": "RUNNING",
            "regiao": "sa-east-1",
        }
        item = transform_config(config)
        assert item["PK"] == "MediaLive#channel"
        assert item["SK"] == "1234567"
        assert item["servico"] == "MediaLive"
        assert item["nome_canal"] == "0001_WARNER"
        assert item["channel_id"] == "1234567"
        assert item["tipo"] == "channel"
        assert item["estado"] == "RUNNING"
        assert item["regiao"] == "sa-east-1"
        assert "updated_at" in item
        # data is a JSON string containing the original
        parsed = json.loads(item["data"])
        assert parsed["servico"] == "MediaLive"

    def test_defaults_for_missing_fields(self):
        item = transform_config({})
        assert item["PK"] == "Unknown#channel"
        assert item["SK"] == ""
        assert item["servico"] == "Unknown"
        assert item["nome_canal"] == ""


# ── transform_log ─────────────────────────────────────────────

class TestTransformLog:
    def test_basic_log(self):
        evento = {
            "canal": "0001_WARNER",
            "servico_origem": "MediaLive",
            "timestamp": "2024-07-15T12:00:00Z",
            "metrica_nome": "ActiveAlerts",
            "metrica_valor": 3,
            "severidade": "WARNING",
            "tipo_erro": "ALERTA_ATIVO",
        }
        item = transform_log(evento)
        assert item["PK"] == "MediaLive#0001_WARNER"
        assert item["SK"] == "2024-07-15T12:00:00Z#ActiveAlerts"
        assert item["severidade"] == "WARNING"
        assert item["tipo_erro"] == "ALERTA_ATIVO"
        assert item["canal"] == "0001_WARNER"
        assert item["servico_origem"] == "MediaLive"
        assert item["metrica_nome"] == "ActiveAlerts"
        assert item["metrica_valor"] == Decimal("3")
        assert isinstance(item["ttl"], int)
        assert item["ttl"] > 0
        parsed = json.loads(item["data"])
        assert parsed["canal"] == "0001_WARNER"

    def test_defaults_for_missing_fields(self):
        item = transform_log({})
        assert item["PK"] == "Unknown#unknown"
        assert item["SK"] == "#"
        assert item["severidade"] == "INFO"


# ── _is_within_last_n_days ────────────────────────────────────

class TestIsWithinLastNDays:
    def test_recent_file_included(self):
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        )
        key = (
            f"kb-logs/MediaLive/"
            f"CANAL_{yesterday.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        assert _is_within_last_n_days(key, 30) is True

    def test_old_file_excluded(self):
        old = datetime.now(timezone.utc) - timedelta(days=60)
        key = (
            f"kb-logs/MediaLive/"
            f"CANAL_{old.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        assert _is_within_last_n_days(key, 30) is False

    def test_unparseable_key_included(self):
        assert _is_within_last_n_days(
            "kb-logs/MediaLive/weird.json", 30,
        ) is True


# ── parse_args ────────────────────────────────────────────────

class TestParseArgs:
    def test_defaults(self):
        with patch.dict(
            "os.environ",
            {"KB_CONFIG_BUCKET": "b1", "KB_LOGS_BUCKET": "b2"},
        ):
            args = parse_args([])
        assert args.config_bucket == "b1"
        assert args.logs_bucket == "b2"
        assert args.configs_table == "StreamingConfigs"
        assert args.logs_table == "StreamingLogs"

    def test_cli_overrides(self):
        args = parse_args([
            "--config-bucket", "my-cfg",
            "--logs-bucket", "my-logs",
            "--configs-table", "T1",
            "--logs-table", "T2",
        ])
        assert args.config_bucket == "my-cfg"
        assert args.logs_bucket == "my-logs"
        assert args.configs_table == "T1"
        assert args.logs_table == "T2"


# ── migrate_configs ───────────────────────────────────────────

class TestMigrateConfigs:
    def _make_s3(self, keys, bodies):
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": k} for k in keys
                ],
            },
        ]
        s3.get_paginator.return_value = paginator
        s3.get_object = MagicMock(
            side_effect=lambda **kw: {
                "Body": MagicMock(
                    read=MagicMock(
                        return_value=json.dumps(
                            bodies[kw["Key"]],
                        ).encode(),
                    ),
                ),
            },
        )
        return s3

    def test_migrates_all_configs(self):
        keys = [
            "kb-config/MediaLive/ch1.json",
            "kb-config/MediaLive/ch2.json",
        ]
        bodies = {
            keys[0]: {
                "servico": "MediaLive",
                "tipo": "channel",
                "channel_id": "1",
            },
            keys[1]: {
                "servico": "MediaLive",
                "tipo": "channel",
                "channel_id": "2",
            },
        }
        s3 = self._make_s3(keys, bodies)
        table = MagicMock()
        batch_ctx = MagicMock()
        table.batch_writer.return_value.__enter__ = (
            MagicMock(return_value=batch_ctx)
        )
        table.batch_writer.return_value.__exit__ = (
            MagicMock(return_value=False)
        )

        count, errors = migrate_configs(s3, table, "bucket")
        assert count == 2
        assert errors == 0
        assert batch_ctx.put_item.call_count == 2


# ── migrate_logs ──────────────────────────────────────────────

class TestMigrateLogs:
    def test_skips_old_logs(self):
        now = datetime.now(timezone.utc)
        recent = now - timedelta(days=5)
        old = now - timedelta(days=60)
        recent_key = (
            "kb-logs/MediaLive/"
            f"CH_{recent.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        old_key = (
            "kb-logs/MediaLive/"
            f"CH_{old.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": recent_key},
                    {"Key": old_key},
                ],
            },
        ]
        s3.get_paginator.return_value = paginator
        s3.get_object = MagicMock(
            return_value={
                "Body": MagicMock(
                    read=MagicMock(
                        return_value=json.dumps({
                            "canal": "CH",
                            "servico_origem": "MediaLive",
                            "timestamp": "2024-07-15T12:00:00Z",
                            "metrica_nome": "X",
                            "metrica_valor": 1,
                        }).encode(),
                    ),
                ),
            },
        )
        table = MagicMock()
        batch_ctx = MagicMock()
        table.batch_writer.return_value.__enter__ = (
            MagicMock(return_value=batch_ctx)
        )
        table.batch_writer.return_value.__exit__ = (
            MagicMock(return_value=False)
        )

        count, errors = migrate_logs(s3, table, "bucket")
        assert count == 1
        assert errors == 0
        # Only the recent file should be written
        assert batch_ctx.put_item.call_count == 1
