"""Tier 1: Tests for installer.migrate_cmd.env_to_toml (.env dict → TOML string)."""

import tomllib

from installer.migrate_cmd import env_to_toml


class TestEnvToTomlGeneral:
    def test_iata_included(self):
        result = env_to_toml({"MCTOMQTT_IATA": "SEA"})
        assert '[general]' in result
        assert 'iata = "SEA"' in result

    def test_iata_xxx_omitted(self):
        result = env_to_toml({"MCTOMQTT_IATA": "XXX"})
        assert "iata" not in result

    def test_log_level_debug_included(self):
        result = env_to_toml({"MCTOMQTT_LOG_LEVEL": "DEBUG"})
        assert 'log_level = "DEBUG"' in result

    def test_log_level_info_omitted(self):
        result = env_to_toml({"MCTOMQTT_LOG_LEVEL": "INFO"})
        assert "log_level" not in result

    def test_sync_time_false_included(self):
        result = env_to_toml({"MCTOMQTT_SYNC_TIME": "false"})
        assert "sync_time = false" in result

    def test_sync_time_true_omitted(self):
        result = env_to_toml({"MCTOMQTT_SYNC_TIME": "true"})
        assert "sync_time" not in result

    def test_sync_time_false_bare_not_quoted(self):
        result = env_to_toml({"MCTOMQTT_SYNC_TIME": "false"})
        assert 'sync_time = "false"' not in result
        assert "sync_time = false" in result


class TestEnvToTomlSerial:
    def test_single_port(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_PORTS": "/dev/ttyACM0"})
        assert "[serial]" in result
        assert 'ports = ["/dev/ttyACM0"]' in result

    def test_multiple_ports(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_PORTS": "/dev/ttyACM0,/dev/ttyUSB0"})
        assert 'ports = ["/dev/ttyACM0", "/dev/ttyUSB0"]' in result

    def test_baud_rate_non_default(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_BAUD_RATE": "9600"})
        assert "baud_rate = 9600" in result

    def test_baud_rate_default_omitted(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_BAUD_RATE": "115200"})
        assert "baud_rate" not in result

    def test_timeout_non_default(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_TIMEOUT": "5"})
        assert "timeout = 5" in result

    def test_timeout_default_omitted(self):
        result = env_to_toml({"MCTOMQTT_SERIAL_TIMEOUT": "2"})
        assert "timeout" not in result


class TestEnvToTomlUpdate:
    def test_repo_and_branch(self):
        result = env_to_toml({
            "MCTOMQTT_UPDATE_REPO": "user/repo",
            "MCTOMQTT_UPDATE_BRANCH": "develop",
        })
        assert "[update]" in result
        assert 'repo = "user/repo"' in result
        assert 'branch = "develop"' in result

    def test_only_repo(self):
        result = env_to_toml({"MCTOMQTT_UPDATE_REPO": "user/repo"})
        assert "[update]" in result
        assert 'repo = "user/repo"' in result

    def test_empty_repo_omitted(self):
        result = env_to_toml({"MCTOMQTT_UPDATE_REPO": ""})
        assert "[update]" not in result


class TestEnvToTomlRemoteSerial:
    def test_enabled_with_companions(self):
        result = env_to_toml({
            "MCTOMQTT_REMOTE_SERIAL_ENABLED": "true",
            "MCTOMQTT_REMOTE_SERIAL_ALLOWED_COMPANIONS": "KEY1,KEY2",
        })
        assert "[remote_serial]" in result
        assert "enabled = true" in result
        assert 'allowed_companions = ["KEY1", "KEY2"]' in result

    def test_enabled_false_omitted(self):
        result = env_to_toml({"MCTOMQTT_REMOTE_SERIAL_ENABLED": "false"})
        assert "[remote_serial]" not in result

    def test_companions_without_enabled_emitted(self):
        result = env_to_toml({
            "MCTOMQTT_REMOTE_SERIAL_ALLOWED_COMPANIONS": "KEY1",
        })
        assert "[remote_serial]" in result
        assert "enabled = false" in result
        assert '"KEY1"' in result


class TestEnvToTomlBrokers:
    def test_letsmesh_us_broker(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt-us-v1.letsmesh.net",
            "MCTOMQTT_MQTT1_PORT": "443",
            "MCTOMQTT_MQTT1_TRANSPORT": "websockets",
            "MCTOMQTT_MQTT1_USE_TLS": "true",
            "MCTOMQTT_MQTT1_TLS_VERIFY": "true",
            "MCTOMQTT_MQTT1_USE_AUTH_TOKEN": "true",
            "MCTOMQTT_MQTT1_TOKEN_AUDIENCE": "mqtt-us-v1.letsmesh.net",
        })
        assert '[[broker]]' in result
        assert 'name = "letsmesh-us"' in result

    def test_letsmesh_eu_broker(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT2_ENABLED": "true",
            "MCTOMQTT_MQTT2_SERVER": "mqtt-eu-v1.letsmesh.net",
            "MCTOMQTT_MQTT2_PORT": "443",
            "MCTOMQTT_MQTT2_TRANSPORT": "websockets",
            "MCTOMQTT_MQTT2_USE_TLS": "true",
            "MCTOMQTT_MQTT2_USE_AUTH_TOKEN": "true",
        })
        assert 'name = "letsmesh-eu"' in result

    def test_custom_broker_name(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_PORT": "1883",
        })
        assert 'name = "custom-1"' in result

    def test_tls_enabled_block(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_USE_TLS": "true",
            "MCTOMQTT_MQTT1_TLS_VERIFY": "true",
        })
        assert "[broker.tls]" in result
        assert "enabled = true" in result

    def test_tls_disabled_no_block(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_USE_TLS": "false",
        })
        assert "[broker.tls]" not in result

    def test_auth_token(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_USE_AUTH_TOKEN": "true",
            "MCTOMQTT_MQTT1_TOKEN_AUDIENCE": "aud",
            "MCTOMQTT_MQTT1_TOKEN_OWNER": "owner123",
            "MCTOMQTT_MQTT1_TOKEN_EMAIL": "test@example.com",
        })
        assert 'method = "token"' in result
        assert 'audience = "aud"' in result
        assert 'owner = "owner123"' in result
        assert 'email = "test@example.com"' in result

    def test_username_password_auth(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_USERNAME": "user",
            "MCTOMQTT_MQTT1_PASSWORD": "pass",
        })
        assert 'method = "password"' in result
        assert 'username = "user"' in result
        assert 'password = "pass"' in result

    def test_no_auth(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
        })
        assert 'method = "none"' in result

    def test_disabled_broker_omitted(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "false",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
        })
        assert "[[broker]]" not in result

    def test_broker_without_server_omitted(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "",
        })
        assert "[[broker]]" not in result

    def test_multiple_brokers(self):
        result = env_to_toml({
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt-us-v1.letsmesh.net",
            "MCTOMQTT_MQTT1_PORT": "443",
            "MCTOMQTT_MQTT1_TRANSPORT": "websockets",
            "MCTOMQTT_MQTT1_USE_TLS": "true",
            "MCTOMQTT_MQTT1_USE_AUTH_TOKEN": "true",
            "MCTOMQTT_MQTT2_ENABLED": "true",
            "MCTOMQTT_MQTT2_SERVER": "mqtt-eu-v1.letsmesh.net",
            "MCTOMQTT_MQTT2_PORT": "443",
            "MCTOMQTT_MQTT2_TRANSPORT": "websockets",
            "MCTOMQTT_MQTT2_USE_TLS": "true",
            "MCTOMQTT_MQTT2_USE_AUTH_TOKEN": "true",
        })
        assert result.count("[[broker]]") == 2
        assert 'name = "letsmesh-us"' in result
        assert 'name = "letsmesh-eu"' in result


class TestEnvToTomlRoundtrip:
    def test_full_roundtrip(self):
        """Generate TOML, parse with tomllib, verify structure."""
        env = {
            "MCTOMQTT_IATA": "SEA",
            "MCTOMQTT_LOG_LEVEL": "DEBUG",
            "MCTOMQTT_SERIAL_PORTS": "/dev/ttyACM0",
            "MCTOMQTT_SERIAL_BAUD_RATE": "9600",
            "MCTOMQTT_UPDATE_REPO": "Cisien/meshcoretomqtt",
            "MCTOMQTT_UPDATE_BRANCH": "main",
            "MCTOMQTT_REMOTE_SERIAL_ENABLED": "true",
            "MCTOMQTT_REMOTE_SERIAL_ALLOWED_COMPANIONS": "KEY1,KEY2",
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt-us-v1.letsmesh.net",
            "MCTOMQTT_MQTT1_PORT": "443",
            "MCTOMQTT_MQTT1_TRANSPORT": "websockets",
            "MCTOMQTT_MQTT1_USE_TLS": "true",
            "MCTOMQTT_MQTT1_TLS_VERIFY": "true",
            "MCTOMQTT_MQTT1_USE_AUTH_TOKEN": "true",
            "MCTOMQTT_MQTT1_TOKEN_AUDIENCE": "mqtt-us-v1.letsmesh.net",
            "MCTOMQTT_MQTT1_TOKEN_OWNER": "AABB" * 16,
            "MCTOMQTT_MQTT1_TOKEN_EMAIL": "test@example.com",
            "MCTOMQTT_MQTT2_ENABLED": "true",
            "MCTOMQTT_MQTT2_SERVER": "mqtt.custom.com",
            "MCTOMQTT_MQTT2_PORT": "1883",
            "MCTOMQTT_MQTT2_USERNAME": "user",
            "MCTOMQTT_MQTT2_PASSWORD": "pass",
        }
        toml_str = env_to_toml(env)
        parsed = tomllib.loads(toml_str)

        assert parsed["general"]["iata"] == "SEA"
        assert parsed["general"]["log_level"] == "DEBUG"
        assert parsed["serial"]["ports"] == ["/dev/ttyACM0"]
        assert parsed["serial"]["baud_rate"] == 9600
        assert parsed["update"]["repo"] == "Cisien/meshcoretomqtt"
        assert parsed["update"]["branch"] == "main"
        assert parsed["remote_serial"]["enabled"] is True
        assert parsed["remote_serial"]["allowed_companions"] == ["KEY1", "KEY2"]

        brokers = parsed["broker"]
        assert len(brokers) == 2
        assert brokers[0]["name"] == "letsmesh-us"
        assert brokers[0]["auth"]["method"] == "token"
        assert brokers[1]["name"] == "custom-2"
        assert brokers[1]["auth"]["method"] == "password"

    def test_empty_env_roundtrip(self):
        """Empty env produces empty (but valid) TOML."""
        toml_str = env_to_toml({})
        # Should be empty or whitespace-only — still valid TOML
        parsed = tomllib.loads(toml_str)
        assert isinstance(parsed, dict)

    def test_minimal_broker_roundtrip(self):
        """Single broker with no auth roundtrips correctly."""
        env = {
            "MCTOMQTT_MQTT1_ENABLED": "true",
            "MCTOMQTT_MQTT1_SERVER": "mqtt.example.com",
            "MCTOMQTT_MQTT1_PORT": "1883",
        }
        toml_str = env_to_toml(env)
        parsed = tomllib.loads(toml_str)
        broker = parsed["broker"][0]
        assert broker["server"] == "mqtt.example.com"
        assert broker["port"] == 1883
        assert broker["auth"]["method"] == "none"
