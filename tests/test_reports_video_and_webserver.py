import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from cli.model import Account, Config, Printer
from web import (
    _read_bed_leveling_grid,
    _read_printer_report,
    _read_printer_settings_summary,
    app,
    webserver,
)
from web.lib.service import RunState


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
    def __init__(self, cfg, config_root=None):
        self.cfg = cfg
        self.config_root = Path(config_root or ".")

    @contextmanager
    def open(self):
        yield self.cfg

    @contextmanager
    def modify(self):
        yield self.cfg


class FakeBorrowServices:
    def __init__(self, mqtt):
        self._mqtt = mqtt

    @contextmanager
    def borrow(self, name):
        assert name == "mqttqueue"
        yield self._mqtt


class FakeVideoServices:
    def __init__(self, videoqueue, payloads):
        self.svcs = {"videoqueue": videoqueue} if videoqueue else {}
        self.refs = {"videoqueue": 0}
        self._payloads = payloads

    def stream(self, name, **kwargs):
        assert name == "videoqueue"
        for payload in self._payloads:
            yield SimpleNamespace(data=payload)


def _base_config(model="V8111", active_index=0):
    return Config(
        account=Account(
            auth_token="token",
            region="eu",
            user_id="user-1",
            email="user@example.com",
        ),
        printers=[_printer(model=model)],
        active_printer_index=active_index,
    )


def _install_app_state(**values):
    keys = [
        "config",
        "api_key",
        "login",
        "printer_index",
        "insecure",
        "video_supported",
        "unsupported_device",
    ]
    old_values = {key: app.config.get(key) for key in keys}
    old_svc = app.svc
    for key, value in values.items():
        if key == "svc":
            app.svc = value
        else:
            app.config[key] = value
    return old_values, old_svc


def _restore_app_state(old_values, old_svc):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value


def test_read_bed_leveling_grid_parses_response_and_persists_snapshot(tmp_path, monkeypatch):
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: object())
    monkeypatch.setattr(
        "cli.mqtt.mqtt_gcode_dump",
        lambda client, gcode, collect_window=4.0: [
            {"resData": "BL-Grid-0 -0.767 -0.642\nnoise\n"},
            {"resData": "BL-Grid-1 -0.500 -0.250\n"},
        ],
    )

    try:
        data, err = _read_bed_leveling_grid()
    finally:
        _restore_app_state(old_values, old_svc)

    assert err is None
    assert data["rows"] == 2
    assert data["cols"] == 2
    assert data["min"] == -0.767
    assert data["max"] == -0.25
    saved = list((tmp_path / "bed_leveling").glob("*.bed"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["grid"][0] == [-0.767, -0.642]


def test_read_bed_leveling_grid_handles_connect_failure(tmp_path, monkeypatch):
    manager = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)

    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: (_ for _ in ()).throw(RuntimeError("boom")))

    try:
        data, err = _read_bed_leveling_grid()
    finally:
        _restore_app_state(old_values, old_svc)

    assert data is None
    assert err[1] == 503
    assert "MQTT connection failed" in err[0]["error"]


def test_read_printer_report_disconnects_client_and_summary_builds_groups(monkeypatch):
    manager = FakeConfigManager(_base_config())
    old_values, old_svc = _install_app_state(config=manager, printer_index=0, insecure=False)
    disconnects = []

    client = SimpleNamespace(_mqtt=SimpleNamespace(disconnect=lambda: disconnects.append(True)))
    monkeypatch.setattr("cli.mqtt.mqtt_open", lambda config, printer_index, insecure: client)
    monkeypatch.setattr(
        "web._collect_printer_gcode_output",
        lambda client, gcode, window, drain: {
            "raw_output": "raw",
            "cleaned_output": "M851 X0 Y0 Z-0.12\nM301 P22.2 I1.08 D114.0",
            "chunks": ["raw"],
            "chunk_count": 1,
        },
    )

    try:
        report = _read_printer_report("probe_offset")
    finally:
        _restore_app_state(old_values, old_svc)

    assert report["name"] == "probe_offset"
    assert report["gcode"] == "M851"
    assert disconnects == [True]

    mqtt = SimpleNamespace(
        get_z_offset_state=lambda: {"available": True, "steps": 25, "mm": 0.25, "source": "cached"},
        refresh_z_offset=lambda timeout=None: {"available": True, "steps": 25, "mm": 0.25, "source": "live"},
    )
    old_values, old_svc = _install_app_state(svc=FakeBorrowServices(mqtt))
    monkeypatch.setattr(
        "web._read_printer_report",
        lambda name: {
            "name": name,
            "label": name,
            "gcode": {"settings": "M503", "probe_offset": "M851", "babystep": "M290 R"}[name],
            "cleaned_output": {
                "settings": "M420 S1 Z10\nM301 P22.2 I1.08 D114.0",
                "probe_offset": "M851 X0.00 Y0.00 Z-0.12",
                "babystep": "M290 Z0.05",
            }[name],
        },
    )

    try:
        summary = _read_printer_settings_summary()
    finally:
        _restore_app_state(old_values, old_svc)

    assert summary["status"] == "ok"
    assert summary["live_z_offset"]["display"] == "0.25 mm"
    assert any(item["command"] == "M851" for item in summary["highlights"])
    assert any(item["command"] == "M290" for item in summary["groups"]["leveling"])


def test_bed_leveling_last_route_reads_latest_saved_grid(tmp_path, monkeypatch):
    client = app.test_client()
    bed_dir = tmp_path / "bed_leveling"
    bed_dir.mkdir()
    (bed_dir / "20260101_100000.bed").write_text(json.dumps({"grid": [[0.1]], "rows": 1, "cols": 1}))
    (bed_dir / "20260101_110000.bed").write_text(json.dumps({"grid": [[0.2]], "rows": 1, "cols": 1}))

    monkeypatch.setattr("web._log_dir", str(tmp_path))
    old_values, old_svc = _install_app_state(login=True, api_key=None)

    try:
        response = client.get("/api/printer/bed-leveling/last")
    finally:
        _restore_app_state(old_values, old_svc)

    assert response.status_code == 200
    assert response.get_json()["grid"] == [[0.2]]
    assert response.get_json()["saved_at"] == "20260101_110000"


def test_video_download_requires_auth_and_streams_when_enabled():
    class FakeVideoQueue:
        def __init__(self):
            self.video_enabled = True
            self.state = RunState.Stopped
            self.start_calls = 0
            self.await_ready_calls = 0

        def start(self):
            self.start_calls += 1
            self.state = RunState.Running

        def await_ready(self):
            self.await_ready_calls += 1

    videoqueue = FakeVideoQueue()
    client = app.test_client()
    old_values, old_svc = _install_app_state(
        api_key=API_KEY,
        login=True,
        video_supported=True,
        svc=FakeVideoServices(videoqueue, [b"abc", b"def"]),
    )

    try:
        unauthorized = client.get("/video")
        authorized = client.get("/video", headers={"X-Api-Key": API_KEY})
    finally:
        _restore_app_state(old_values, old_svc)

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    assert authorized.data == b"abcdef"
    assert videoqueue.start_calls == 1
    assert videoqueue.await_ready_calls == 1


def test_webserver_bootstraps_secret_key_and_skips_services_for_unsupported_device(tmp_path, monkeypatch):
    run_calls = []
    register_calls = []
    resolve_calls = []

    monkeypatch.setattr("web.register_services", lambda flask_app: register_calls.append(flask_app))
    monkeypatch.setattr("web.cli.config.resolve_api_key", lambda config: resolve_calls.append(config) or API_KEY)
    monkeypatch.setattr("web.app.run", lambda host, port: run_calls.append((host, port)))
    monkeypatch.setattr("web.app.context_processor", lambda func: func)
    monkeypatch.setattr("web.os.getenv", lambda key, default=None: default if key != "FLASK_SECRET_KEY" else None)
    monkeypatch.setattr("web.token", lambda length: "stable-secret-key")

    config = FakeConfigManager(_base_config(), config_root=tmp_path)
    old_values, old_svc = _install_app_state()
    old_filaments = getattr(app, "filaments", None)

    try:
        webserver(config, printer_index=0, host="127.0.0.1", port=4470)
        first_api_key = app.config["api_key"]
    finally:
        _restore_app_state(old_values, old_svc)
        app.filaments = old_filaments

    secret_file = tmp_path / "flask_secret.key"
    assert secret_file.read_text() == "stable-secret-key"
    assert register_calls == [app]
    assert resolve_calls == [config]
    assert run_calls == [("127.0.0.1", 4470)]
    assert first_api_key == API_KEY

    register_calls.clear()
    run_calls.clear()
    unsupported_root = tmp_path / "unsupported"
    unsupported_root.mkdir()
    unsupported_config = FakeConfigManager(_base_config(model="V8260"), config_root=unsupported_root)
    old_values, old_svc = _install_app_state()
    old_filaments = getattr(app, "filaments", None)

    try:
        webserver(unsupported_config, printer_index=0, host="0.0.0.0", port=9000)
        unsupported_flag = app.config["unsupported_device"]
    finally:
        _restore_app_state(old_values, old_svc)
        app.filaments = old_filaments

    assert register_calls == []
    assert run_calls == [("0.0.0.0", 9000)]
    assert unsupported_flag is True
