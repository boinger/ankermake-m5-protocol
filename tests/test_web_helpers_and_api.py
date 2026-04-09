import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cli.model import Account, Config, Printer
from libflagship import resolve_root_dir
from web import _AccessLogNoiseFilter, _ConsoleLogBuffer
from web import (
    _build_command_group,
    _build_filament_move_gcode,
    _clean_printer_report_output,
    _configure_request_limits,
    _deep_update,
    _env_int,
    _extract_report_commands,
    _filament_service_length,
    _filament_service_temp,
    _format_signed_mm,
    _probe_printer_storage_files,
    _parse_z_offset_mm,
    _resolve_apprise,
    _safe_same_site_redirect_target,
    _serialize_z_offset_state,
    _z_offset_mm_to_steps,
    _z_offset_steps_to_mm,
    app,
)
import web as web_module
from web.util import flash_redirect


def _printer(sn, name, model="V8111", ip_addr=""):
    return Printer(
        id=sn,
        sn=sn,
        name=name,
        model=model,
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 0, 0),
        wifi_mac="aabbccddeeff",
        ip_addr=ip_addr,
        mqtt_key=b"\x01\x02",
        api_hosts=["api.example"],
        p2p_hosts=["p2p.example"],
        p2p_duid=f"duid-{sn}",
        p2p_key="secret",
    )


class FakeConfigManager:
    def __init__(self, cfg):
        self.cfg = cfg

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


class FakeServices:
    def __init__(self, mqtt=None):
        self._mqtt = mqtt if mqtt is not None else SimpleNamespace(is_printing=False)
        self.restart_calls = 0

    @contextmanager
    def borrow(self, name):
        assert name == "mqttqueue"
        yield self._mqtt

    def restart_all(self, await_ready=False):
        self.restart_calls += 1


def test_deep_update_merges_nested_dicts():
    base = {"a": {"b": 1, "c": 2}, "x": 1}
    updates = {"a": {"c": 3, "d": 4}, "y": 2}

    merged = _deep_update(base, updates)

    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "x": 1, "y": 2}


def test_filament_and_z_offset_helpers():
    assert _filament_service_temp({"nozzle_temp_other_layer": "220"}) == 220
    assert _filament_service_length({"length_mm": "42.345"}, "length_mm") == 42.34
    assert _build_filament_move_gcode(12.5).splitlines()[1] == "G1 E12.5 F240"
    assert _z_offset_steps_to_mm(37) == 0.37
    assert _z_offset_mm_to_steps(0.37) == 37
    assert _format_signed_mm(-0.12) == "-0.12"
    assert _parse_z_offset_mm({"offset": "0.456"}, "offset") == 0.46
    assert _serialize_z_offset_state({"steps": 25, "source": "mqtt"})["display"] == "0.25 mm"


def test_resolve_apprise_drops_progress_max_value():
    cfg = SimpleNamespace(
        notifications={
            "apprise": {
                "enabled": True,
                "progress": {"max_value": 123, "interval_percent": 10},
            }
        }
    )

    apprise = _resolve_apprise(cfg)

    assert apprise["enabled"] is True
    assert "max_value" not in apprise["progress"]
    assert apprise["progress"]["interval_percent"] == 10


def test_clean_report_output_and_extract_commands():
    raw = "\x1b[31mok\r\nM301 P22.2 I1.08 D114.0\r\n+ringbuf stuff\r\nM851 X0.00 Y0.00 Z-0.12\r\n"

    cleaned = _clean_printer_report_output(raw)
    commands = _extract_report_commands(cleaned, "M420 S1 Z10")
    group = _build_command_group(commands, ["M851", "M420", "M301"])

    assert cleaned == "M301 P22.2 I1.08 D114.0\nM851 X0.00 Y0.00 Z-0.12"
    assert commands["M301"] == "M301 P22.2 I1.08 D114.0"
    assert [item["command"] for item in group] == ["M851", "M420", "M301"]


def test_env_int_rejects_invalid_and_too_small_values(caplog):
    assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={}) == 20

    with caplog.at_level("WARNING", logger="web"):
        assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={"UPLOAD_MAX_FORM_PARTS": "abc"}) == 20
        assert _env_int("UPLOAD_MAX_FORM_PARTS", 20, env={"UPLOAD_MAX_FORM_PARTS": "0"}) == 20

    assert "Ignoring invalid integer value for UPLOAD_MAX_FORM_PARTS" in caplog.text
    assert "Ignoring UPLOAD_MAX_FORM_PARTS='0' because it is smaller than 1" in caplog.text


def test_configure_request_limits_separates_file_and_form_limits():
    flask_app = SimpleNamespace(config={})

    _configure_request_limits(
        flask_app,
        env={
            "UPLOAD_MAX_MB": "4096",
            "UPLOAD_MAX_FORM_MEMORY_KB": "1024",
            "UPLOAD_MAX_FORM_PARTS": "12",
        },
    )

    assert flask_app.config["MAX_CONTENT_LENGTH"] == 4096 * 1024 * 1024
    assert flask_app.config["MAX_FORM_MEMORY_SIZE"] == 1024 * 1024
    assert flask_app.config["MAX_FORM_PARTS"] == 12


def test_resolve_root_dir_supports_bundled_and_source_layouts(tmp_path):
    assert resolve_root_dir(
        frozen=False,
        file_path="/tmp/ankerctl/libflagship/__init__.py",
    ) == Path("/tmp/ankerctl")

    assert resolve_root_dir(
        frozen=True,
        meipass=str(tmp_path / "bundle"),
    ) == (tmp_path / "bundle").resolve()

    assert resolve_root_dir(
        frozen=True,
        meipass=None,
        executable=str(tmp_path / "dist" / "ankerctl"),
    ) == (tmp_path / "dist").resolve()


def test_flash_redirect_requires_path_and_redirects():
    with app.test_request_context("/"):
        response = flash_redirect("/next", "Saved", "success")
        assert response.location.endswith("/next")

    with app.test_request_context("/"):
        try:
            flash_redirect("")
        except ValueError as exc:
            assert "Redirect path is required" in str(exc)
        else:
            raise AssertionError("flash_redirect should reject empty path")


def test_api_health_and_version_routes():
    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    app.config["login"] = True
    app.config["api_key"] = None

    try:
        health = client.get("/api/health")
        version = client.get("/api/version")

        assert health.status_code == 200
        assert health.get_json() == {"status": "ok"}
        assert version.status_code == 200
        assert version.get_json()["server"] == "1.9.0"
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key


def test_console_log_buffer_supports_recent_tail_and_incremental_updates():
    buffer = _ConsoleLogBuffer(max_lines=3)
    for idx in range(1, 6):
        buffer.append(f"line {idx}")

    recent = buffer.snapshot(limit=10)
    incremental = buffer.snapshot(after_id=3, limit=10)
    truncated = buffer.snapshot(after_id=1, limit=10)

    assert [entry["text"] for entry in recent["entries"]] == ["line 3", "line 4", "line 5"]
    assert recent["first_id"] == 3
    assert recent["last_id"] == 5
    assert [entry["text"] for entry in incremental["entries"]] == ["line 4", "line 5"]
    assert incremental["truncated"] is False
    assert truncated["truncated"] is True


def test_access_log_noise_filter_suppresses_console_polling_and_static_assets():
    filt = _AccessLogNoiseFilter()

    console_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /api/console/logs?limit=200&after=108 HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )
    static_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /static/ankersrv.js HTTP/1.1" 304 -',
        args=(), exc_info=None,
    )
    api_record = logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=0,
        msg='127.0.0.1 - - [08/Apr/2026 17:57:47] "GET /api/health HTTP/1.1" 200 -',
        args=(), exc_info=None,
    )

    assert filt.filter(console_record) is False
    assert filt.filter(static_record) is False
    assert filt.filter(api_record) is True


def test_api_console_logs_returns_recent_entries(monkeypatch):
    calls = []

    class FakeConsoleBuffer:
        def snapshot(self, *, limit=200, after_id=None):
            calls.append((limit, after_id))
            return {
                "entries": [{"id": 42, "text": "[*] printer ready"}],
                "first_id": 40,
                "last_id": 42,
                "next_after": 42,
                "truncated": False,
                "max_lines": 2000,
            }

    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    monkeypatch.setattr(web_module, "_get_console_log_buffer", lambda: FakeConsoleBuffer())
    app.config["login"] = True
    app.config["api_key"] = None

    try:
        response = client.get("/api/console/logs?limit=25&after=10")
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key

    assert response.status_code == 200
    assert response.get_json()["entries"] == [{"id": 42, "text": "[*] printer ready"}]
    assert calls == [(25, 10)]


def test_api_printers_and_switch_active_printer(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[
            _printer("SN1", "Printer One", ip_addr="192.168.1.10"),
            _printer("SN2", "Printer Two", ip_addr="192.168.1.11"),
        ],
    )
    manager = FakeConfigManager(cfg)
    services = FakeServices()
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }
    old_svc = app.svc

    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = "secret-key-123456"
    app.svc = services

    try:
        printers = client.get("/api/printers", headers={"X-Api-Key": "secret-key-123456"})
        unauthorized = client.post("/api/printers/active", json={"index": 1})
        switched = client.post(
            "/api/printers/active",
            json={"index": 1},
            headers={"X-Api-Key": "secret-key-123456"},
        )

        assert printers.status_code == 200
        assert printers.get_json()["active_index"] == 0
        assert len(printers.get_json()["printers"]) == 2

        assert unauthorized.status_code == 401
        assert "Unauthorized" in unauthorized.get_json()["error"]

        assert switched.status_code == 200
        assert switched.get_json()["printer"]["index"] == 1
        assert app.config["printer_index"] == 1
        assert cfg.active_printer_index == 1
        assert services.restart_calls == 1
    finally:
        app.svc = old_svc
        for key, value in old_values.items():
            app.config[key] = value


def test_root_shows_ffmpeg_warning_only_for_camera_capable_devices(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer One", model="V8111")],
    )
    manager = FakeConfigManager(cfg)
    client = app.test_client()

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "printer_index_locked": app.config.get("printer_index_locked"),
        "video_supported": app.config.get("video_supported"),
        "login": app.config.get("login"),
        "unsupported_device": app.config.get("unsupported_device"),
        "api_key": app.config.get("api_key"),
    }

    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["printer_index_locked"] = False
    app.config["video_supported"] = True
    app.config["login"] = True
    app.config["unsupported_device"] = False
    app.config["api_key"] = None

    try:
        monkeypatch.setattr("web._ffmpeg_available", lambda: False)
        camera = client.get("/")
        app.config["video_supported"] = False
        no_camera = client.get("/")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    assert camera.status_code == 200
    assert "Camera features need `ffmpeg`" in camera.get_data(as_text=True)
    assert no_camera.status_code == 200
    assert "Camera features need `ffmpeg`" not in no_camera.get_data(as_text=True)


def test_probe_printer_storage_files_uses_one_shot_mqtt_probe(monkeypatch):
    cfg = Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer("SN1", "Printer One", ip_addr="192.168.1.10")],
    )
    manager = FakeConfigManager(cfg)
    disconnects = []
    fake_client = SimpleNamespace(_mqtt=SimpleNamespace(disconnect=lambda: disconnects.append(True)))

    old_values = {
        "config": app.config.get("config"),
        "printer_index": app.config.get("printer_index"),
        "insecure": app.config.get("insecure"),
    }
    app.config["config"] = manager
    app.config["printer_index"] = 0
    app.config["insecure"] = False

    monkeypatch.setattr("web.cli.mqtt.mqtt_open", lambda config, printer_index, insecure: fake_client)
    monkeypatch.setattr(
        "web.cli.mqtt.mqtt_file_list_probe",
        lambda client, source, source_value, timeout, collect_window: {
            "request": {"commandType": 1009, "value": 1},
            "source_value": 1,
            "reply_count": 1,
            "replies": [{"commandType": 1009, "reply": 0}],
            "files": [{"name": "cube.gcode", "path": "/usr/data/local/model/cube.gcode", "timestamp": 123, "source": "onboard"}],
        },
    )

    try:
        result, error = _probe_printer_storage_files(source="onboard")
    finally:
        for key, value in old_values.items():
            app.config[key] = value

    assert error is None
    assert result["source_value"] == 1
    assert result["files"][0]["path"] == "/usr/data/local/model/cube.gcode"
    assert disconnects == [True]


def test_printer_control_guard_without_login():
    client = app.test_client()
    old_login = app.config.get("login")
    old_api_key = app.config.get("api_key")
    app.config["login"] = False
    app.config["api_key"] = None
    try:
        response = client.get("/api/printer/bed-leveling")
        assert response.status_code == 503
        assert "No printer configured" in response.get_json()["error"]
    finally:
        app.config["login"] = old_login
        app.config["api_key"] = old_api_key


def test_safe_same_site_redirect_target_rejects_external_style_paths():
    with app.test_request_context("/"):
        assert _safe_same_site_redirect_target("//evil.example/path", {"x": ["1"]}) == "/"
        assert _safe_same_site_redirect_target("https:/evil.example", None) == "/"


def test_apikey_url_param_redirects_and_sets_session():
    """?apikey= should validate the key, strip itself from the URL, and
    bootstrap the browser session auth used by the web UI."""
    client = app.test_client()
    old_api_key = app.config.get("api_key")
    app.config["api_key"] = "test-secret-key"
    try:
        resp = client.get("/api/health?apikey=test-secret-key&foo=bar")
        assert resp.status_code == 302, f"Expected redirect, got {resp.status_code}"
        assert "apikey" not in resp.headers.get("Location", "")
        assert resp.headers.get("Location", "").endswith("/api/health?foo=bar")

        with client.session_transaction() as sess:
            assert sess.get("authenticated"), \
                "Session should be authenticated after ?apikey= redirect"
    finally:
        app.config["api_key"] = old_api_key
