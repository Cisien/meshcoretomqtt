"""Tier 2: Tests for installer.migrate_cmd.parse_env_file with real temp files."""

from installer.migrate_cmd import parse_env_file


class TestParseEnvFile:
    def test_standard_key_value(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY1=value1\nKEY2=value2\n")
        result = parse_env_file(str(f))
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_comments_skipped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# This is a comment\nKEY=value\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_blank_lines_skipped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY1=value1\n\n\nKEY2=value2\n")
        result = parse_env_file(str(f))
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_lines_without_equals_skipped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY1=value1\nNOEQUALS\nKEY2=value2\n")
        result = parse_env_file(str(f))
        assert result == {"KEY1": "value1", "KEY2": "value2"}

    def test_whitespace_stripped(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("  KEY  =  value  \n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value"}

    def test_nonexistent_path(self):
        result = parse_env_file("/nonexistent/path/.env")
        assert result == {}

    def test_empty_string_path(self):
        result = parse_env_file("")
        assert result == {}

    def test_empty_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("")
        result = parse_env_file(str(f))
        assert result == {}

    def test_value_with_equals(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("KEY=value=with=equals\n")
        result = parse_env_file(str(f))
        assert result == {"KEY": "value=with=equals"}

    def test_keys_with_spaces_preserved(self, tmp_path):
        """Keys with spaces in name are preserved (matches bash behavior)."""
        f = tmp_path / ".env"
        f.write_text("KEY WITH SPACES=value\n")
        result = parse_env_file(str(f))
        assert result == {"KEY WITH SPACES": "value"}

    def test_mixed_content(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text(
            "# Header comment\n"
            "MCTOMQTT_IATA=SEA\n"
            "\n"
            "# Another comment\n"
            "MCTOMQTT_LOG_LEVEL=DEBUG\n"
            "BADLINE\n"
            "MCTOMQTT_SERIAL_PORTS=/dev/ttyACM0\n"
        )
        result = parse_env_file(str(f))
        assert result == {
            "MCTOMQTT_IATA": "SEA",
            "MCTOMQTT_LOG_LEVEL": "DEBUG",
            "MCTOMQTT_SERIAL_PORTS": "/dev/ttyACM0",
        }
