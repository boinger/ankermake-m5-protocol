import json
from types import SimpleNamespace

from click.testing import CliRunner

import ankerctl
import cli.mqtt
from libflagship.mqtt import MqttMsgType


class FakeConfigManager:
    def __init__(self):
        self.api_keys = []
        self.removed = 0

    def set_api_key(self, key):
        self.api_keys.append(key)

    def remove_api_key(self):
        self.removed += 1


def test_main_configures_logging_and_skips_upgrade_for_http(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    logging_calls = []
    upgrades = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: logging_calls.append((level, log_dir)))
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: upgrades.append("upgrade"))
    monkeypatch.setattr("ankerctl.libflagship.seccode.calc_check_code", lambda duid, mac: "CODE123")

    result = runner.invoke(
        ankerctl.main,
        ["-v", "--printer", "2", "http", "calc-check-code", "DUID", "11:22:33:44:55:66"],
    )

    assert result.exit_code == 0
    assert "check_code: CODE123" in result.output
    assert logging_calls and logging_calls[0][0] == 10
    assert upgrades == []


def test_mqtt_send_blocks_dangerous_commands_without_force(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    opened = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: opened.append(True))

    result = runner.invoke(
        ankerctl.main,
        ["mqtt", "send", "ZZ_MQTT_CMD_RECOVER_FACTORY"],
    )

    assert result.exit_code == 1
    assert opened == []


def test_mqtt_monitor_can_subscribe_to_command_topics(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = SimpleNamespace(subscribed=False, wildcard=None)

    def subscribe_device_topics(wildcard=False):
        fake_client.subscribed = True
        fake_client.wildcard = wildcard
        return ["/device/maker/SN123/command", "/device/maker/SN123/query"]

    def fetchloop():
        yield (
            SimpleNamespace(topic="/device/maker/SN123/command", payload=b"payload"),
            [{"commandType": 1026, "axis": "xy"}],
        )

    fake_client.subscribe_device_topics = subscribe_device_topics
    fake_client.fetchloop = fetchloop

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)

    result = runner.invoke(ankerctl.main, ["mqtt", "monitor", "--command-topics"])

    assert result.exit_code == 0
    assert fake_client.subscribed is True
    assert fake_client.wildcard is False
    assert "[1026] move_zero" in result.output
    assert "{'axis': 'xy'}" in result.output


def test_mqtt_monitor_can_sniff_wildcard_command_topics(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = SimpleNamespace(subscribed=False, wildcard=None)

    def subscribe_device_topics(wildcard=False):
        fake_client.subscribed = True
        fake_client.wildcard = wildcard
        return ["/device/maker/SN123/command", "/device/maker/SN123/query", "/device/maker/SN123/#"]

    def fetchloop():
        yield (
            SimpleNamespace(topic="/device/maker/SN123/command", payload=b"payload"),
            [{"commandType": 1025, "value": 3}],
        )

    fake_client.subscribe_device_topics = subscribe_device_topics
    fake_client.fetchloop = fetchloop

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)

    result = runner.invoke(ankerctl.main, ["mqtt", "monitor", "--sniff-topics"])

    assert result.exit_code == 0
    assert fake_client.subscribed is True
    assert fake_client.wildcard is True
    assert ankerctl.mqtt_topic_direction("/device/maker/SN123/command") == "app->printer"
    assert "[1025] move_direction" in result.output


def test_mqtt_file_list_probe_defaults_to_onboard_and_collects_replies(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = object()
    calls = []

    def mqtt_collect_command(client, msg, timeout, collect_window):
        calls.append((client, msg, timeout, collect_window))
        return [{"commandType": msg["commandType"], "value": msg["value"], "files": ["local.gcode"]}]

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_collect_command", mqtt_collect_command)

    result = runner.invoke(ankerctl.main, ["mqtt", "file-list-probe"])

    assert result.exit_code == 0
    assert calls == [
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 1,
            },
            10.0,
            3.0,
        )
    ]
    assert "Probing file list with value=1 (printer/onboard)" in result.output
    assert '"reply_count": 1' in result.output
    assert "local.gcode" in result.output


def test_mqtt_file_list_probe_uses_usb_default_value_and_allows_override(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    fake_client = object()
    calls = []

    def mqtt_collect_command(client, msg, timeout, collect_window):
        calls.append((client, msg, timeout, collect_window))
        return [{"commandType": msg["commandType"], "value": msg["value"], "files": ["usb.gcode"]}]

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_open", lambda *args, **kwargs: fake_client)
    monkeypatch.setattr("ankerctl.cli.mqtt.mqtt_collect_command", mqtt_collect_command)

    usb_result = runner.invoke(ankerctl.main, ["mqtt", "file-list-probe", "--source", "usb"])
    override_result = runner.invoke(
        ankerctl.main,
        ["mqtt", "file-list-probe", "--source", "usb", "--value", "2", "--timeout", "5", "--window", "1.5"],
    )

    assert usb_result.exit_code == 0
    assert override_result.exit_code == 0
    assert calls == [
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 0,
            },
            10.0,
            3.0,
        ),
        (
            fake_client,
            {
                "commandType": MqttMsgType.ZZ_MQTT_CMD_FILE_LIST_REQUEST.value,
                "value": 2,
            },
            5.0,
            1.5,
        ),
    ]
    assert "value=0 (usb/thumb drive candidate)" in usb_result.output
    assert "value=2 (usb/thumb drive candidate)" in override_result.output


def test_parse_file_list_replies_filters_mismatched_storage_paths():
    result = cli.mqtt.parse_file_list_replies(
        [{
            "commandType": 1009,
            "fileLists": json.dumps([
                {"name": "cube.gcode", "path": "/usr/data/local/model/cube.gcode", "timestamp": 123},
                {"name": "usb.gcode", "path": "/tmp/udisk/udisk1/usb.gcode", "timestamp": 456},
            ]),
        }],
        requested_source="onboard",
    )

    assert result["reply_count"] == 1
    assert result["files"] == [
        {
            "name": "cube.gcode",
            "path": "/usr/data/local/model/cube.gcode",
            "timestamp": 123,
            "source": "onboard",
        }
    ]


def test_http_calc_sec_code_and_webserver_run_dispatch(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()
    webserver_calls = []

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.Environment.load_config", lambda self, required=True: None)
    monkeypatch.setattr("ankerctl.libflagship.seccode.create_check_code_v1", lambda duid, mac: (123, "SEC456"))
    monkeypatch.setattr("web.webserver", lambda config, printer_index, host, port, insecure, **kwargs: webserver_calls.append((config, printer_index, host, port, insecure, kwargs)))

    sec_result = runner.invoke(
        ankerctl.main,
        ["http", "calc-sec-code", "DUID", "11:22:33:44:55:66"],
    )
    run_result = runner.invoke(
        ankerctl.main,
        ["--insecure", "--pppp-dump", "trace.log", "--printer", "1", "webserver", "run", "--host", "0.0.0.0", "--port", "7788"],
    )

    assert sec_result.exit_code == 0
    assert "sec_ts:   123" in sec_result.output
    assert "sec_code: SEC456" in sec_result.output
    assert run_result.exit_code == 0
    assert webserver_calls == [
        (fake_config, 1, "0.0.0.0", 7788, True, {"pppp_dump": "trace.log"})
    ]


def test_config_password_commands_validate_generate_and_remove(monkeypatch):
    runner = CliRunner()
    fake_config = FakeConfigManager()

    monkeypatch.setattr("ankerctl.cli.config.configmgr", lambda: fake_config)
    monkeypatch.setattr("ankerctl.cli.logfmt.setup_logging", lambda level, log_dir=None: None)
    monkeypatch.setattr("ankerctl.Environment.upgrade_config_if_needed", lambda self: None)
    monkeypatch.setattr("ankerctl.cli.config.validate_api_key", lambda key: (False, "bad key"))
    monkeypatch.setattr("ankerctl.secrets.token_hex", lambda size: "RANDOMKEY1234567890")

    invalid = runner.invoke(ankerctl.main, ["config", "set-password", "bad"])
    generated = runner.invoke(ankerctl.main, ["config", "set-password"])
    removed = runner.invoke(ankerctl.main, ["config", "remove-password"])

    assert invalid.exit_code == 1
    assert generated.exit_code == 0
    assert removed.exit_code == 0
    assert fake_config.api_keys == ["RANDOMKEY1234567890"]
    assert fake_config.removed == 1


class FakeConfigContext:
    """Minimal config manager for testing mqtt_open/pppp_open bounds checks."""
    def __init__(self, printers):
        self._printers = printers

    def open(self):
        return self

    def __enter__(self):
        return SimpleNamespace(
            printers=self._printers,
            account=SimpleNamespace(region="eu", auth_token="fake", user_id="fake"),
        )

    def __exit__(self, *args):
        pass


def test_mqtt_open_raises_on_invalid_printer_index():
    import cli.mqtt
    config = FakeConfigContext(printers=[])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.mqtt.mqtt_open(config, printer_index=0, insecure=False)


def test_pppp_open_raises_on_invalid_printer_index():
    import cli.pppp
    config = FakeConfigContext(printers=[])
    import pytest
    with pytest.raises(ValueError, match="out of range"):
        cli.pppp.pppp_open(config, printer_index=0)
