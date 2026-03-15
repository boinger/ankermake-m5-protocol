from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

from cli.model import Account, Config, Printer
import web as web_module
from web import app
from web.service.filament import FilamentStore


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
    def __init__(self, mqtt):
        self._mqtt = mqtt
        self.svcs = {"mqttqueue": mqtt}

    @contextmanager
    def borrow(self, name):
        assert name == "mqttqueue"
        yield self._mqtt


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


def _install_state(tmp_path, mqtt):
    old_values = {
        "config": app.config.get("config"),
        "api_key": app.config.get("api_key"),
        "login": app.config.get("login"),
        "printer_index": app.config.get("printer_index"),
        "unsupported_device": app.config.get("unsupported_device"),
    }
    old_svc = app.svc
    old_filaments = getattr(app, "filaments", None)
    old_swap = getattr(app, "filament_swap_state", None)

    app.config["config"] = FakeConfigManager(_base_config())
    app.config["api_key"] = API_KEY
    app.config["login"] = True
    app.config["printer_index"] = 0
    app.config["unsupported_device"] = False
    app.svc = FakeServices(mqtt)
    app.filaments = FilamentStore(tmp_path / "filaments.db")
    app.filament_swap_state = None

    return old_values, old_svc, old_filaments, old_swap


def _restore_state(old_values, old_svc, old_filaments, old_swap):
    app.svc = old_svc
    for key, value in old_values.items():
        app.config[key] = value
    app.filaments = old_filaments
    app.filament_swap_state = old_swap


def test_filament_crud_and_apply_routes(tmp_path):
    sent = []
    mqtt = SimpleNamespace(is_printing=False, send_gcode=lambda gcode: sent.append(gcode), nozzle_temp=220)
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        created = client.post(
            "/api/filaments",
            json={
                "name": "PLA Test",
                "nozzle_temp": 215,
                "nozzle_temp_first_layer": 215,
                "bed_temp": 60,
            },
            headers={"X-Api-Key": API_KEY},
        )
        profile_id = created.get_json()["id"]
        listed = client.get("/api/filaments")
        updated = client.put(
            f"/api/filaments/{profile_id}",
            json={"name": "PLA Test 2", "bed_temp": 65},
            headers={"X-Api-Key": API_KEY},
        )
        duplicated = client.post(
            f"/api/filaments/{profile_id}/duplicate",
            headers={"X-Api-Key": API_KEY},
        )
        applied = client.post(
            f"/api/filaments/{profile_id}/apply",
            headers={"X-Api-Key": API_KEY},
        )
        deleted = client.delete(
            f"/api/filaments/{profile_id}",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert created.status_code == 201
    assert any(item["name"] == "PLA Test" for item in listed.get_json()["filaments"])
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "PLA Test 2"
    assert duplicated.status_code == 201
    assert duplicated.get_json()["name"].startswith("PLA Test 2")
    assert applied.status_code == 200
    assert applied.get_json()["gcode"] == "M104 S215\nM140 S65"
    assert deleted.status_code == 200
    assert sent == ["M104 S215\nM140 S65"]


def test_filament_service_preheat_and_move_routes(tmp_path):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=220,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        profile = app.filaments.create({"name": "PLA", "nozzle_temp": 220})
        app.config["config"].cfg.filament_service["quick_move_length_mm"] = 12.5
        preheat = client.post(
            "/api/filaments/service/preheat",
            json={"profile_id": profile["id"]},
            headers={"X-Api-Key": API_KEY},
        )
        move = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "retract", "length_mm": 25},
            headers={"X-Api-Key": API_KEY},
        )
        move_default = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "extrude"},
            headers={"X-Api-Key": API_KEY},
        )
        invalid = client.post(
            "/api/filaments/service/move",
            json={"profile_id": profile["id"], "action": "spin"},
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert preheat.status_code == 200
    assert move.status_code == 200
    assert move_default.status_code == 200
    assert invalid.status_code == 400
    assert sent[0] == "M104 S220"
    assert "G1 E-25 F2700" in sent[1]
    assert "G1 E12.5 F900" in sent[2]


def test_filament_swap_routes_follow_manual_guided_flow_by_default(tmp_path):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=150,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)

    try:
        started = client.post(
            "/api/filaments/service/swap/start",
            headers={"X-Api-Key": API_KEY},
        )
        token = started.get_json()["swap"]["token"]
        confirmed = client.post(
            "/api/filaments/service/swap/confirm",
            json={"token": token},
            headers={"X-Api-Key": API_KEY},
        )
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert started.get_json()["pending"] is True
    assert started.get_json()["swap"]["mode"] == "manual"
    assert started.get_json()["swap"]["phase"] == "await_manual_swap"
    assert confirmed.status_code == 200
    assert confirmed.get_json()["pending"] is False
    assert cancelled.status_code == 200
    assert cancelled.get_json()["pending"] is False
    assert sent == ["M104 S140"]


def test_filament_swap_routes_cover_legacy_start_confirm_and_cancel(tmp_path, monkeypatch):
    sent = []
    mqtt = SimpleNamespace(
        is_printing=False,
        nozzle_temp=230,
        send_gcode=lambda gcode: sent.append(gcode),
    )
    client = app.test_client()
    old_values, old_svc, old_filaments, old_swap = _install_state(tmp_path, mqtt)
    app.config["config"].cfg.filament_service["allow_legacy_swap"] = True
    app.config["config"].cfg.filament_service["swap_unload_length_mm"] = 55
    app.config["config"].cfg.filament_service["swap_load_length_mm"] = 65
    background_calls = []

    def fake_start_background(target, token):
        background_calls.append((target, token))
        return SimpleNamespace()

    monkeypatch.setattr(web_module, "_filament_swap_start_background", fake_start_background)

    try:
        unload = app.filaments.create({"name": "PLA Black", "nozzle_temp": 220, "retract_speed": 40})
        load = app.filaments.create({"name": "PETG White", "nozzle_temp": 240, "retract_speed": 12})
        started = client.post(
            "/api/filaments/service/swap/start",
            json={
                "unload_profile_id": unload["id"],
                "load_profile_id": load["id"],
            },
            headers={"X-Api-Key": API_KEY},
        )
        start_target, token = background_calls.pop()
        start_target(token)
        mismatch = client.post(
            "/api/filaments/service/swap/cancel",
            json={"token": "wrong-token"},
            headers={"X-Api-Key": API_KEY},
        )
        confirmed = client.post(
            "/api/filaments/service/swap/confirm",
            json={"token": token},
            headers={"X-Api-Key": API_KEY},
        )
        confirm_target, confirm_token = background_calls.pop()
        confirm_target(confirm_token)
        cancelled = client.post(
            "/api/filaments/service/swap/cancel",
            headers={"X-Api-Key": API_KEY},
        )
    finally:
        _restore_state(old_values, old_svc, old_filaments, old_swap)

    assert started.status_code == 200
    assert started.get_json()["pending"] is True
    assert started.get_json()["swap"]["mode"] == "legacy"
    assert mismatch.status_code == 409
    assert confirmed.status_code == 200
    assert confirmed.get_json()["pending"] is True
    assert cancelled.status_code == 200
    assert cancelled.get_json()["pending"] is False
    assert "G1 E-55 F240" in sent[0]
    assert "G1 E65 F240" in sent[1]
