from contextlib import contextmanager
from datetime import datetime
import subprocess
from types import SimpleNamespace

from cli.model import Account, Config, Printer
from web import app


API_KEY = "secret-key-123456"


def _printer(sn="SN1", name="Printer", model="V8111"):
    return Printer(
        id=sn,
        sn=sn,
        name=name,
        model=model,
        create_time=datetime(2024, 1, 1, 12, 0, 0),
        update_time=datetime(2024, 1, 1, 12, 0, 0),
        wifi_mac="aabbccddeeff",
        ip_addr="192.168.1.10",
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
    def __init__(self, **services):
        self._services = services
        self.svcs = {name: svc for name, svc in services.items() if svc is not None}

    @contextmanager
    def borrow(self, name):
        yield self._services.get(name)


def _base_config():
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer()],
    )


def _install_app_state(*, mqtt=None, videoqueue=None, config=None, login=True, video_supported=True, unsupported=False):
    old_values = {
        "config": app.config.get("config"),
        "api_key": app.config.get("api_key"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
        "video_supported": app.config.get("video_supported"),
        "unsupported_device": app.config.get("unsupported_device"),
    }
    old_svc = app.svc

    app.config["config"] = FakeConfigManager(config or _base_config())
    app.config["api_key"] = API_KEY
    app.config["login"] = login
    app.config["printer_index"] = 0
    app.config["video_supported"] = video_supported
    app.config["unsupported_device"] = unsupported
    app.svc = FakeServices(mqttqueue=mqtt, videoqueue=videoqueue)

    return old_values, old_svc


def _restore_app_state(old_values, old_svc):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value


def test_printer_gcode_route_normalizes_safe_commands_and_blocks_motion_while_printing():
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        normal = client.post(
            "/api/printer/gcode",
            json={"gcode": "G28 ; home\n\nM104 S200\n"},
            headers={"X-Api-Key": API_KEY},
        )
        mqtt.is_printing = True
        blocked = client.post(
            "/api/printer/gcode",
            json={"gcode": "G1 X10 Y10"},
            headers={"X-Api-Key": API_KEY},
        )
        safe = client.post(
            "/api/printer/gcode",
            json={"gcode": "M117 Printing"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert normal.status_code == 200
    assert safe.status_code == 200
    assert blocked.status_code == 409
    assert sent == ["G28\nM104 S200", "M117 Printing"]


def test_printer_control_and_autolevel_routes_validate_and_dispatch():
    control_calls = []
    autolevel_calls = []
    home_calls = []
    mqtt = SimpleNamespace(
        is_printing=False,
        send_print_control=lambda value: control_calls.append(value),
        send_auto_leveling=lambda: autolevel_calls.append(True),
        send_home=lambda axis: home_calls.append(axis),
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        invalid = client.post(
            "/api/printer/control",
            json={"value": "abc"},
            headers={"X-Api-Key": API_KEY},
        )
        control = client.post(
            "/api/printer/control",
            json={"value": 2},
            headers={"X-Api-Key": API_KEY},
        )
        allowed = client.post(
            "/api/printer/autolevel",
            headers={"X-Api-Key": API_KEY},
        )
        home = client.post(
            "/api/printer/home",
            json={"axis": "xy"},
            headers={"X-Api-Key": API_KEY},
        )
        bad_home = client.post(
            "/api/printer/home",
            json={"axis": "bad"},
            headers={"X-Api-Key": API_KEY},
        )
        mqtt.is_printing = True
        blocked = client.post(
            "/api/printer/autolevel",
            headers={"X-Api-Key": API_KEY},
        )
        blocked_home = client.post(
            "/api/printer/home",
            json={"axis": "z"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert invalid.status_code == 400
    assert control.status_code == 200
    assert allowed.status_code == 200
    assert home.status_code == 200
    assert bad_home.status_code == 400
    assert blocked.status_code == 409
    assert blocked_home.status_code == 409
    assert control_calls == [2]
    assert autolevel_calls == [True]
    assert home_calls == ["xy"]


def test_printer_z_offset_routes_refresh_set_and_nudge():
    send_calls = []
    state = {"available": True, "steps": 12, "mm": 0.12, "source": "cached", "seq": 1}

    def refresh_z_offset(timeout=None):
        return dict(state)

    def wait_for_target(target_steps, after_seq=None, timeout=None):
        state["steps"] = target_steps
        state["mm"] = round(target_steps / 100.0, 2)
        state["seq"] = after_seq + 1
        state["source"] = "confirmed"
        return dict(state)

    mqtt = SimpleNamespace(
        refresh_z_offset=refresh_z_offset,
        wait_for_z_offset_target=wait_for_target,
        send_gcode=lambda gcode: send_calls.append(gcode),
        get_z_offset_state=lambda: {"available": False, "steps": None, "mm": None, "source": "cached"},
    )
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        get_resp = client.get("/api/printer/z-offset", headers={"X-Api-Key": API_KEY})
        refresh_resp = client.post(
            "/api/printer/z-offset/refresh",
            headers={"X-Api-Key": API_KEY},
        )
        set_resp = client.post(
            "/api/printer/z-offset",
            json={"target_mm": 0.15},
            headers={"X-Api-Key": API_KEY},
        )
        nudge_resp = client.post(
            "/api/printer/z-offset/nudge",
            json={"delta_mm": -0.02},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert get_resp.status_code == 200
    assert get_resp.get_json()["z_offset"]["display"] == "0.12 mm"
    assert refresh_resp.status_code == 200
    assert set_resp.status_code == 200
    assert set_resp.get_json()["delta"]["display"] == "+0.03 mm"
    assert nudge_resp.status_code == 200
    assert nudge_resp.get_json()["nudge"]["display"] == "-0.02 mm"
    assert send_calls == ["M290 Z+0.03", "M500", "M290 Z-0.02", "M500"]


def test_history_routes_require_auth_and_clear_entries():
    calls = []
    history = SimpleNamespace(
        get_history=lambda limit, offset: calls.append(("get", limit, offset)) or [{"filename": "cube.gcode"}],
        get_count=lambda: 3,
        clear=lambda: calls.append(("clear",)),
    )
    mqtt = SimpleNamespace(history=history)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        unauthorized = client.get("/api/history")
        authorized = client.get("/api/history?limit=999&offset=-5", headers={"X-Api-Key": API_KEY})
        cleared = client.delete("/api/history", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.get_json()["total"] == 3
    assert cleared.status_code == 200
    assert calls == [("get", 500, 0), ("clear",)]


def test_timelapse_routes_list_download_delete_and_reject_traversal(tmp_path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    video_path = capture_dir / "cube.mp4"
    video_path.write_bytes(b"fake-mp4")

    timelapse = SimpleNamespace(
        enabled=True,
        _captures_dir=str(capture_dir),
        list_videos=lambda: [{"filename": "cube.mp4", "size": 8}],
        get_video_path=lambda filename: str(video_path) if filename == "cube.mp4" else None,
        delete_video=lambda filename: filename == "cube.mp4",
    )
    mqtt = SimpleNamespace(timelapse=timelapse)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt)

    try:
        listed = client.get("/api/timelapses", headers={"X-Api-Key": API_KEY})
        invalid = client.get("/api/timelapse/..\\\\passwd.mp4", headers={"X-Api-Key": API_KEY})
        downloaded = client.get("/api/timelapse/cube.mp4", headers={"X-Api-Key": API_KEY})
        deleted = client.delete("/api/timelapse/cube.mp4", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert listed.status_code == 200
    assert listed.get_json()["enabled"] is True
    assert invalid.status_code == 400
    assert downloaded.status_code == 200
    assert downloaded.data == b"fake-mp4"
    assert deleted.status_code == 200


def test_snapshot_route_reports_expected_error_paths(monkeypatch):
    client = app.test_client()
    old_values, old_svc = _install_app_state(video_supported=False, videoqueue=None)

    try:
        not_supported = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._ffmpeg_path", lambda: None)
    old_values, old_svc = _install_app_state(video_supported=True, videoqueue=object())
    try:
        no_ffmpeg = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    old_values, old_svc = _install_app_state(video_supported=True, videoqueue=None)
    try:
        no_service = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=False),
    )
    try:
        video_disabled = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._video_has_recent_frame", lambda vq: False)
    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=True, last_frame_at=None),
    )
    try:
        no_frames = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    monkeypatch.setattr("web._video_has_recent_frame", lambda vq: True)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))
        ),
    )
    old_values, old_svc = _install_app_state(
        video_supported=True,
        videoqueue=SimpleNamespace(video_enabled=True, last_frame_at=123.0),
    )
    try:
        timeout = client.get("/api/snapshot", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert not_supported.status_code == 400
    assert no_ffmpeg.status_code == 500
    assert no_service.status_code == 503
    assert video_disabled.status_code == 409
    assert "Enable video" in video_disabled.get_json()["error"]
    assert no_frames.status_code == 409
    assert "no live camera frames" in no_frames.get_json()["error"]
    assert timeout.status_code == 504
    assert "timed out" in timeout.get_json()["error"]
    assert "Command" not in timeout.get_json()["error"]


def test_unsupported_device_guard_blocks_printer_control_routes():
    mqtt = SimpleNamespace(send_print_control=lambda value: None)
    client = app.test_client()
    old_values, old_svc = _install_app_state(mqtt=mqtt, unsupported=True)

    try:
        response = client.post(
            "/api/printer/control",
            json={"value": 1},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 503
    assert "not supported" in response.get_json()["error"]
