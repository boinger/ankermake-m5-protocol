from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from cli.config import (
    AnkerConfigManager,
    merge_config_preferences,
    resolve_api_key,
    update_empty_printer_ips,
    validate_api_key,
)
from cli.model import Account, Config, Printer


def _sample_printer(sn="SN-1", ip_addr=""):
    return Printer(
        id="printer-1",
        sn=sn,
        name="Printer",
        model="V8111",
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 30, 0),
        wifi_mac="aabbccddeeff",
        ip_addr=ip_addr,
        mqtt_key=b"\x01\x02",
        api_hosts=["api.example"],
        p2p_hosts=["p2p.example"],
        p2p_duid="duid-1",
        p2p_key="secret",
    )


def _sample_config(**overrides):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="printer@example.com",
        ),
        printers=[_sample_printer()],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def test_validate_api_key_checks_length_and_charset():
    assert validate_api_key("valid_key-123456")[0] is True
    assert validate_api_key("short")[0] is False
    assert validate_api_key("bad key with spaces")[0] is False


def test_resolve_api_key_prefers_environment(monkeypatch):
    monkeypatch.setenv("ANKERCTL_API_KEY", "valid_key-123456")
    config = SimpleNamespace(get_api_key=lambda: "from-config")

    assert resolve_api_key(config) == "valid_key-123456"


def test_resolve_api_key_exits_on_invalid_environment_value(monkeypatch):
    monkeypatch.setenv("ANKERCTL_API_KEY", "bad")
    config = SimpleNamespace(get_api_key=lambda: "from-config")

    with pytest.raises(SystemExit):
        resolve_api_key(config)


def test_merge_config_preferences_preserves_user_settings():
    existing = _sample_config(
        upload_rate_mbps=50,
        notifications={
            "apprise": {
                "enabled": True,
                "server_url": "https://notify.example",
                "events": {
                    "print_started": False,
                },
            }
        },
    )
    new_config = _sample_config(upload_rate_mbps=10, notifications={})

    merged = merge_config_preferences(existing, new_config)

    assert merged.upload_rate_mbps == 50
    assert merged.notifications["apprise"]["enabled"] is True
    assert merged.notifications["apprise"]["server_url"] == "https://notify.example"
    assert merged.notifications["apprise"]["events"]["print_finished"] is True
    assert merged.notifications["apprise"]["events"]["print_started"] is False


def test_update_empty_printer_ips_only_fills_missing_values():
    cfg = _sample_config()
    cfg.printers.append(_sample_printer(sn="SN-2", ip_addr="192.168.1.25"))

    @contextmanager
    def modify():
        yield cfg

    manager = SimpleNamespace(modify=modify)

    update_empty_printer_ips(manager, {"SN-1": "192.168.1.10", "SN-2": "192.168.1.20"})

    assert cfg.printers[0].ip_addr == "192.168.1.10"
    assert cfg.printers[1].ip_addr == "192.168.1.25"


def test_config_manager_round_trips_serialized_config(tmp_path):
    dirs = SimpleNamespace(user_config_path=tmp_path)
    manager = AnkerConfigManager(dirs, classes=(Config, Account, Printer))
    cfg = _sample_config()

    manager.save("default", cfg)
    loaded = manager.load("default", None)

    assert isinstance(loaded, Config)
    assert loaded.account.email == "printer@example.com"
    assert loaded.printers[0].mqtt_key == b"\x01\x02"
