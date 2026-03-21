import io
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest

from cli.model import Account, Config, Printer
from libflagship.httpapi import APIError
from web import app
from web.config import ConfigImportError, config_import, config_login, config_show
from web.service.filetransfer import FileTransferService


def _config():
    return Config(
        account=Account(
            auth_token="very-secret-token",
            region="eu",
            user_id="user-1234567890",
            email="user@example.com",
            country="DE",
        ),
        printers=[
            Printer(
                id="printer-1",
                sn="SN123",
                name="Printer",
                model="V8111",
                create_time=datetime(2024, 1, 1, 12, 0, 0),
                update_time=datetime(2024, 1, 1, 12, 0, 0),
                wifi_mac="aabbccddeeff",
                ip_addr="192.168.1.10",
                mqtt_key=b"\x01\x02",
                api_hosts=["api.example"],
                p2p_hosts=["p2p.example"],
                p2p_duid="duid-1",
                p2p_key="secret",
            )
        ],
    )


def test_config_show_redacts_secrets():
    rendered = config_show(_config())

    assert "[REDACTED]" in rendered
    assert "very-secret-token" not in rendered
    assert "user@example.com" in rendered
    assert "aa:bb:cc:dd:ee:ff" in rendered


def test_config_import_handles_invalid_login_and_api_errors(monkeypatch):
    login_file = SimpleNamespace(stream=io.BytesIO(b"not-json"))

    with pytest.raises(ConfigImportError, match="Failed to parse login file"):
        config_import(login_file, object())

    valid_login = SimpleNamespace(stream=io.BytesIO(b'{"data": {"auth_token": "x"}}'))
    monkeypatch.setattr("web.config.cli.config.import_config_from_server", lambda config, data, insecure: (_ for _ in ()).throw(APIError("boom")))

    with pytest.raises(ConfigImportError, match="auth token might be expired"):
        config_import(valid_login, object())


def test_config_login_handles_captcha_and_success(monkeypatch):
    imported = []

    def fake_import(config, login, insecure):
        imported.append((config, login, insecure))

    monkeypatch.setattr("web.config.cli.config.import_config_from_server", fake_import)

    captcha_error = APIError(
        "captcha required",
        json={"data": {"captcha_id": "cap-1", "item": "img-data"}},
    )
    monkeypatch.setattr("web.config.cli.config.fetch_config_by_login", lambda *args, **kwargs: (_ for _ in ()).throw(captcha_error))

    with pytest.raises(ConfigImportError) as excinfo:
        config_login("user@example.com", "pw", "DE", None, None, object())

    assert excinfo.value.captcha == {"id": "cap-1", "img": "img-data"}

    monkeypatch.setattr("web.config.cli.config.fetch_config_by_login", lambda *args, **kwargs: {"auth_token": "abc", "ab_code": "DE"})
    config_login("user@example.com", "pw", "DE", None, None, object())

    assert imported and imported[0][1]["auth_token"] == "abc"


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg


@contextmanager
def _borrow_value(value):
    yield value


def test_filetransfer_notify_apprise_upload():
    svc = object.__new__(FileTransferService)
    calls = []
    svc._notifier = SimpleNamespace(send=lambda event, payload=None: calls.append((event, payload)))

    svc._notify_apprise_upload("cube.gcode", 2048, True)

    assert calls == [("gcode_uploaded", {"filename": "cube.gcode", "size": "2.0 KB", "size_bytes": 2048, "start_print": True})]


def test_filetransfer_send_file_happy_path(monkeypatch):
    svc = object.__new__(FileTransferService)
    svc.PROGRESS_INTERVAL = 0.0
    notifications = []
    apprise_calls = []
    svc._notify_upload = lambda payload: notifications.append(payload)
    svc._notify_apprise_upload = lambda filename, size_bytes, start_print: apprise_calls.append((filename, size_bytes, start_print))

    mqtt = SimpleNamespace(set_gcode_layer_count=lambda count: notifications.append({"layer_count": count}))
    old_svc = app.svc
    old_config = app.config.get("config")
    old_printer_index = app.config.get("printer_index")
    old_pppp_dump = app.config.get("pppp_dump")
    app.svc = SimpleNamespace(borrow=lambda name: _borrow_value(mqtt))
    app.config["config"] = FakeConfigManager(_config())
    app.config["printer_index"] = 0
    app.config["pppp_dump"] = None

    fake_api = SimpleNamespace(
        aabb_request=lambda *args, **kwargs: notifications.append({"print_started": True}),
        stop=lambda: notifications.append({"stopped": True}),
    )

    send_calls = []
    monkeypatch.setattr("web.service.filetransfer.extract_layer_count", lambda raw: 12)
    monkeypatch.setattr("web.service.filetransfer.patch_gcode_time", lambda raw: raw + b";TIME:1")
    monkeypatch.setattr("web.service.filetransfer.cli.pppp.pppp_open", lambda *args, **kwargs: fake_api)
    monkeypatch.setattr(
        "web.service.filetransfer.cli.pppp.pppp_send_file",
        lambda api, fui, data, rate_limit_mbps=None, progress_cb=None, show_progress=True: (
            send_calls.append((fui.name, data, rate_limit_mbps)),
            progress_cb(len(data), len(data)),
        ),
    )

    fd = SimpleNamespace(read=lambda: b"G28\n", filename="cube.gcode")

    try:
        svc.send_file(fd, user_name="alice", rate_limit_mbps=25, start_print=True)
    finally:
        app.svc = old_svc
        app.config["config"] = old_config
        app.config["printer_index"] = old_printer_index
        app.config["pppp_dump"] = old_pppp_dump

    assert send_calls and send_calls[0][0] == "cube.gcode"
    assert any(item.get("status") == "start" and item.get("start_print") is True for item in notifications if isinstance(item, dict))
    assert any(item.get("layer_count") == 12 for item in notifications if isinstance(item, dict))
    assert any(item.get("status") == "done" and item.get("start_print") is True for item in notifications if isinstance(item, dict))
    assert any(item.get("print_started") is True for item in notifications if isinstance(item, dict))
    assert any(item.get("stopped") is True for item in notifications if isinstance(item, dict))
    assert apprise_calls == [("cube.gcode", 11, True)]
