import time
from types import SimpleNamespace

from web.service.mqtt import MqttQueue, PrintState


def _queue():
    queue = object.__new__(MqttQueue)
    queue._ha = SimpleNamespace(enabled=True, update_state=lambda **kwargs: ha_updates.append(kwargs))
    queue._notifier = SimpleNamespace(
        is_event_enabled=lambda event: True,
        progress_interval=lambda default=25: 25,
        progress_max=lambda: None,
    )
    queue._history = SimpleNamespace(
        record_start=lambda *args, **kwargs: history_calls.append(("start", args, kwargs)),
        record_finish=lambda *args, **kwargs: history_calls.append(("finish", args, kwargs)),
        record_fail=lambda *args, **kwargs: history_calls.append(("fail", args, kwargs)),
    )
    queue._timelapse = SimpleNamespace(
        start_capture=lambda filename="unknown": timelapse_calls.append(("start", filename)),
        finish_capture=lambda final=False: timelapse_calls.append(("finish", final)),
        fail_capture=lambda: timelapse_calls.append(("fail",)),
        enabled=True,
        _capture_thread=None,
    )
    queue._send_event = lambda event, payload, include_image=False: events.append((event, payload, include_image))
    queue._z_offset_steps = None
    queue._z_offset_updated_at = 0.0
    queue._z_offset_seq = 0
    queue._z_offset_cond = __import__("threading").Condition()
    queue._state_lock = __import__("threading").RLock()
    queue._gcode_layer_count = None
    queue._last_message_time = 0.0
    queue._nozzle_temp = None
    queue._nozzle_temp_target = None
    queue._bed_temp = None
    queue._bed_temp_target = None
    queue._control_username = "tester@example.com"
    queue._debug_log_payloads = False
    queue._reset_print_state()
    return queue


def test_forward_to_ha_updates_temperatures_and_progress():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})
    queue._state = PrintState.PRINTING
    queue._forward_to_ha({
        "commandType": 1001,
        "progress": 50,
        "name": "cube.gcode",
        "totalTime": 120,
        "time": 60,
    })

    assert {"nozzle_temp": 215, "nozzle_temp_target": 220} in ha_updates
    assert {"bed_temp": 65, "bed_temp_target": 70} in ha_updates
    assert any(update.get("print_progress") == 50 and update.get("print_filename") == "cube.gcode" for update in ha_updates)


def test_forward_to_ha_keeps_local_temperature_state_when_ha_is_disabled():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._ha.enabled = False

    queue._forward_to_ha({"commandType": 1003, "currentTemp": 21500, "targetTemp": 22000})
    queue._forward_to_ha({"commandType": 1004, "currentTemp": 6500, "targetTemp": 7000})

    assert queue.nozzle_temp == 215
    assert queue.nozzle_temp_target == 220
    assert queue._bed_temp == 65
    assert queue._bed_temp_target == 70
    assert ha_updates == []


def test_emit_progress_respects_bucket_interval():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PRINTING

    payload = {"name": "cube.gcode"}
    queue._emit_progress(payload, 10)
    queue._emit_progress(payload, 20)
    queue._emit_progress(payload, 26)
    queue._emit_progress(payload, 49)
    queue._emit_progress(payload, 50)

    assert [event[0] for event in events] == ["print_progress", "print_progress"]
    assert events[0][1]["percent"] == 26
    assert events[1][1]["percent"] == 50


def test_handle_notification_start_finish_and_failure_paths(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 0})
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/fail.gcode"})
    queue._handle_notification({
        "commandType": 1001,
        "progress": 25,
        "name": "fail.gcode",
        "errorMessage": "jam",
    })
    queue._handle_notification({
        "commandType": 1001,
        "progress": 26,
        "name": "fail.gcode",
        "errorMessage": "jam",
    })

    assert history_calls[0][0] == "start"
    assert history_calls[1][0] == "finish"
    assert [call[0] for call in history_calls[:4]] == ["start", "finish", "start", "fail"]
    assert timelapse_calls[0] == ("start", "cube.gcode")
    assert timelapse_calls[1] == ("finish", True)
    assert timelapse_calls[2] == ("start", "fail.gcode")
    assert timelapse_calls[3] == ("fail",)
    assert [event[0] for event in events[:5]] == [
        "print_started",
        "print_finished",
        "print_started",
        "print_progress",
        "print_failed",
    ]


def test_deferred_filename_starts_timelapse_after_print_state(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert queue._pending_history_start is True
    assert history_calls == []
    assert timelapse_calls == []

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/deferred.gcode"})

    assert queue._pending_history_start is False
    assert history_calls == [("start", ("deferred.gcode",), {"task_id": None})]
    assert timelapse_calls == [("start", "deferred.gcode")]


def test_handle_notification_aborts_active_print_on_value_8(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/active.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 8})

    assert history_calls == [
        ("start", ("active.gcode",), {"task_id": None}),
        ("fail", (), {"filename": "active.gcode", "reason": "aborted", "task_id": None}),
    ]
    assert timelapse_calls == [("start", "active.gcode"), ("fail",)]
    assert events[-1][0] == "print_failed"
    assert events[-1][2] is False
    assert events[-1][1]["filename"] == "active.gcode"
    assert events[-1][1]["reason"] == "aborted"
    assert queue.get_state()["print"]["state"] == 0


def test_send_print_control_shotgun_during_prepare_state():
    # During the prepare phase (ct=1000 value=8, not yet active), stop sends both
    # value=0 and value=4 in nested+flat form for firmware compatibility.
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    queue._handle_notification({"commandType": 1000, "value": 8})

    queue.send_print_control(4)

    assert sent == [
        {"commandType": 1008, "data": {"value": 0, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 0},
        {"commandType": 1008, "data": {"value": 4, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 4},
    ]
    assert queue._stop_requested is True


def test_send_pause_resume_print_control_uses_nested_and_flat_payloads():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    queue._state = PrintState.PRINTING

    queue.send_print_control(2)
    queue._state = PrintState.PAUSED
    queue.send_print_control(3)

    assert sent == [
        {"commandType": 1008, "data": {"value": 2, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 2},
        {"commandType": 1008, "data": {"value": 3, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 3},
    ]
    assert queue._stop_requested is False


def test_send_gcode_dedupes_identical_g28_commands(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))
    now = [100.0]
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: now[0])
    monkeypatch.setattr("web.service.mqtt.time.sleep", lambda seconds: None)

    queue.send_gcode("G28")
    queue.send_gcode("G28")
    queue.send_gcode("G28 Z")
    now[0] += 10.1
    queue.send_gcode("G28")

    assert [cmd["cmdData"] for cmd in sent] == ["G28", "G28 Z", "G28"]


def test_send_home_uses_native_move_zero_payloads():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.send_home("xy")
    queue.send_home("z")
    queue.send_home("all")

    assert sent == [
        {"commandType": 1026, "value": 0},
        {"commandType": 1026, "value": 2},
        {"commandType": 1026, "value": 2},
    ]


def test_1044_captures_filename_without_marking_prepare_state():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/prepare.gcode"})

    state = queue.get_state()["print"]
    assert state["preparing"] is False
    assert state["active"] is False
    assert state["last_filename"] == "prepare.gcode"


def test_value_8_marks_prepare_state_before_print_start():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1000, "value": 8})

    state = queue.get_state()["print"]
    assert state["preparing"] is True
    assert state["active"] is False


def test_upload_only_idle_transition_does_not_create_fake_print_history():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/upload-only.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == []
    assert timelapse_calls == []
    assert events == []
    assert queue.get_state()["print"]["preparing"] is False


def test_prepare_state_cancels_on_value_zero_after_stop():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({
        "commandType": 1001,
        "progress": 0,
        "name": "warmup.gcode",
        "task_id": "task-1",
    })
    queue._handle_notification({"commandType": 1000, "value": 8})
    queue._stop_requested = True
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == [("fail", (), {"filename": "warmup.gcode", "reason": "cancelled", "task_id": "task-1"})]
    assert timelapse_calls == [("fail",)]
    assert events == [("print_failed", {"filename": "warmup.gcode", "percent": 0, "elapsed_seconds": "", "remaining_seconds": "", "duration_seconds": "", "elapsed": "", "remaining": "", "duration": "", "reason": "cancelled"}, False)]
    assert queue.get_state()["print"]["preparing"] is False


def test_pending_start_stop_cancels_before_print_becomes_active():
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.mark_pending_print_start("queued.gcode")

    assert queue.get_state()["print"]["pending_start"] is True

    queue.send_print_control(4)
    queue._handle_notification({"commandType": 1000, "value": 0})

    # pending_start counts as pre_start_window → shotgun (value=0 and value=4, nested+flat)
    assert sent == [
        {"commandType": 1008, "data": {"value": 0, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 0},
        {"commandType": 1008, "data": {"value": 4, "userName": "tester@example.com"}},
        {"commandType": 1008, "value": 4},
    ]
    assert history_calls == [("fail", (), {"filename": "queued.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert events == [("print_failed", {"filename": "queued.gcode", "percent": 0, "elapsed_seconds": "", "remaining_seconds": "", "duration_seconds": "", "elapsed": "", "remaining": "", "duration": "", "reason": "cancelled"}, False)]
    assert queue.get_state()["print"]["pending_start"] is False
    assert queue._stop_requested is False


def test_early_stop_during_pre_print_window_sends_value4_and_cancels():
    """Stop during G28/calibration (ct=1044 received, ct=1000 value=1 not yet) must send
    value=4 (not value=0) and correctly record cancellation when the printer confirms with
    ct=1000 value=0."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    sent = []
    queue.client = SimpleNamespace(command=lambda payload: sent.append(payload))

    queue.mark_pending_print_start("cube.gcode")
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})

    # Printer is now in pre-print window: active=True, in_pre_print_window=True
    state = queue.get_state()["print"]
    assert state["active"] is True
    assert state["in_pre_print_window"] is True

    # G28 calibration phase arrives
    queue._handle_notification({"commandType": 1000, "value": 8})
    assert queue.get_state()["print"]["active"] is True  # still active, not aborted

    # User clicks Stop
    queue.send_print_control(4)

    # Must send flat value=4, not value=0
    assert sent[-1] == {"commandType": 1008, "value": 4}

    # Printer confirms cancel
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert history_calls == [("fail", (), {"filename": "cube.gcode", "reason": "cancelled", "task_id": None})]
    assert timelapse_calls == [("fail",)]
    assert queue._stop_requested is False
    assert queue.get_state()["print"]["active"] is False
    assert queue.get_state()["print"]["in_pre_print_window"] is False


def test_build_payload_get_state_and_simulate_event(monkeypatch):
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 150.0)
    queue._state = PrintState.PRINTING
    queue._print_started_at = 100.0
    queue._last_filename = "cube.gcode"

    built = queue._build_payload({"elapsed": 20, "remaining": 30}, 40)
    state_before = queue.get_state()
    queue.set_debug_logging(True)
    queue.simulate_event("start", {"filename": "simulated.gcode"})
    queue.simulate_event("finish", {"filename": "simulated.gcode"})
    queue.simulate_event("fail", {"filename": "simulated.gcode"})

    assert built["filename"] == "cube.gcode"
    assert built["duration_seconds"] == 50
    assert state_before["print"]["active"] is True
    assert queue.get_state()["debug_logging"] is True
    assert [call[0] for call in history_calls[-3:]] == ["start", "finish", "fail"]


def test_out_of_order_ct1001_before_ct1000():
    """ct=1001 (progress) arriving before ct=1000 (state=1) must not
    produce duplicate history records or duplicate PRINT_STARTED events.
    The consolidated _transition_to_active() guards against this."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Progress arrives first (out of order) — should activate print
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,       # 25% on the 0-10000 scale
        "name": "cube.gcode",
    })

    assert queue._state == PrintState.PRINTING
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 1, f"Expected 1 start event, got {len(start_events)}"
    assert len(start_records) == 1, f"Expected 1 history start, got {len(start_records)}"

    # State change arrives later — should be a no-op (already active)
    events.clear()
    history_calls.clear()
    queue._handle_notification({"commandType": 1000, "value": 1})

    late_start_events = [e for e in events if e[0] == "print_started"]
    late_start_records = [c for c in history_calls if c[0] == "start"]
    assert len(late_start_events) == 0, "ct=1000 should not fire a second start event"
    assert len(late_start_records) == 0, "ct=1000 should not record a second history start"


def test_pre_print_window_upgrades_to_full_print():
    """When ct=1044 activates the pre-print window (G28/calibration),
    ct=1000 value=1 should upgrade to full print activation with all
    side effects (history, timelapse, event)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Simulate the pre-print window state set by ct=1044
    queue._state = PrintState.PRE_PRINT
    queue._last_filename = "cube.gcode"

    # ct=1000 value=1 should upgrade from pre-print to full print
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 1, "Pre-print upgrade should fire start event"
    assert len(start_records) == 1, "Pre-print upgrade should record history start"
    assert len(timelapse_calls) > 0, "Pre-print upgrade should start timelapse"


def test_ct1001_blocked_during_pre_print_window():
    """Progress messages (ct=1001) during the pre-print window should NOT
    upgrade to full print activation. Only ct=1000 value=1 does that."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Simulate the pre-print window state set by ct=1044
    queue._state = PrintState.PRE_PRINT
    queue._last_filename = "cube.gcode"

    # ct=1001 progress should be blocked (state is PRE_PRINT, not IDLE)
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
    })

    # Should still be in pre-print window, no activation side effects
    assert queue._state == PrintState.PRE_PRINT, "Pre-print window should not be cleared by ct=1001"
    start_events = [e for e in events if e[0] == "print_started"]
    start_records = [c for c in history_calls if c[0] == "start"]
    assert len(start_events) == 0, "ct=1001 should not fire start event during pre-print"
    assert len(start_records) == 0, "ct=1001 should not record history during pre-print"


def test_print_state_enum_values():
    """PrintState enum has all expected members."""
    assert set(PrintState.__members__.keys()) == {
        "IDLE", "PREPARING", "PRE_PRINT", "PRINTING", "PAUSED", "FAILED",
    }


def test_state_transitions_through_full_lifecycle(monkeypatch):
    """Walk through IDLE -> PREPARING -> PRE_PRINT -> PRINTING -> DONE -> IDLE
    and verify state at each step.  Also verify that _transition_to_active()
    returns False from FAILED and DONE states."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    monkeypatch.setattr("web.service.mqtt.time.monotonic", lambda: 100.0)

    # Start: IDLE
    assert queue._state == PrintState.IDLE

    # IDLE -> PREPARING (via mark_pending_print_start)
    queue.mark_pending_print_start("lifecycle.gcode")
    assert queue._state == PrintState.PREPARING

    # PREPARING -> PRE_PRINT (via ct=1044 with pending start)
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/lifecycle.gcode"})
    assert queue._state == PrintState.PRE_PRINT

    # PRE_PRINT -> PRINTING (via ct=1000 value=1)
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    # PRINTING -> IDLE (via ct=1000 value=0, normal end)
    queue._handle_notification({"commandType": 1000, "value": 0})
    assert queue._state == PrintState.IDLE

    # Verify _transition_to_active() allowed from FAILED (new print after failure)
    queue._state = PrintState.FAILED
    assert queue._transition_to_active({}, progress=0) is True
    assert queue._state == PrintState.PRINTING


def test_state_after_reset_is_always_idle():
    """_reset_print_state() returns to IDLE regardless of prior state."""
    queue = _queue()

    for state in PrintState:
        queue._state = state
        queue._reset_print_state()
        assert queue._state == PrintState.IDLE, f"Expected IDLE after reset from {state.name}"


def test_duplicate_ct1001_failure_only_fires_once():
    """A second ct=1001 with errorMessage on the same print session
    must not produce a second failure event.  _failure_sent guards this."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Activate a print via ct=1001 progress
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
    })
    assert queue._state == PrintState.PRINTING

    # First failure — should fire
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    fail_events = [e for e in events if e[0] == "print_failed"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_events) == 1, f"Expected 1 fail event, got {len(fail_events)}"
    assert len(fail_records) == 1, f"Expected 1 fail record, got {len(fail_records)}"
    assert queue._failure_sent is True

    # Second failure — should be suppressed by _failure_sent
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    dup_fail_events = [e for e in events if e[0] == "print_failed"]
    dup_fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(dup_fail_events) == 0, "Second failure should be suppressed"
    assert len(dup_fail_records) == 0, "Second failure should not record history"


def test_new_print_starts_after_ct1001_failure():
    """After a ct=1001 failure leaves state as FAILED, a new print must
    be able to start.  _transition_to_active() must allow activation
    from FAILED state (equivalent to old _print_active=False behavior)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Activate and then fail via ct=1001
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "first.gcode",
    })
    queue._handle_notification({
        "commandType": 1001,
        "progress": 2500,
        "name": "first.gcode",
        "errorMessage": "jam",
    })
    assert queue._state == PrintState.FAILED

    # New print starts via ct=1001 progress — must not be blocked by FAILED state
    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 500,
        "name": "second.gcode",
    })

    assert queue._state == PrintState.PRINTING, f"Expected PRINTING, got {queue._state}"
    start_events = [e for e in events if e[0] == "print_started"]
    assert len(start_events) == 1, "New print after failure should fire start event"


def test_pause_and_resume():
    """ct=1000 value=2 pauses a printing job, value=3 resumes it."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start a print
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    # Pause
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED
    assert queue.is_printing is True, "Paused print should still count as active"
    assert any(u.get("print_status") == "paused" for u in ha_updates), "HA should report paused"

    # Resume
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 3})
    assert queue._state == PrintState.PRINTING
    assert any(u.get("print_status") == "printing" for u in ha_updates), "HA should report printing after resume"


def test_pause_and_resume_when_resume_ack_is_printing_value():
    """Some firmware confirms resume with ct=1000 value=1 instead of value=3."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    history_calls.clear()
    timelapse_calls.clear()
    events.clear()
    ha_updates.clear()
    queue._handle_notification({"commandType": 1000, "value": 1})

    assert queue._state == PrintState.PRINTING
    assert any(u.get("print_status") == "printing" for u in ha_updates), "HA should report printing after resume"
    assert history_calls == []
    assert timelapse_calls == []
    assert events == []


def test_pause_only_from_printing():
    """value=2 is ignored if not currently PRINTING."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # value=2 from IDLE should be a no-op
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.IDLE


def test_resume_only_from_paused():
    """value=3 is ignored if not currently PAUSED."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start printing, then try resume without pausing first
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    assert queue._state == PrintState.PRINTING

    queue._handle_notification({"commandType": 1000, "value": 3})
    assert queue._state == PrintState.PRINTING, "Resume without pause should be no-op"


def test_stop_during_pause():
    """Stopping a paused print should cancel it properly."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # Start, pause, then stop
    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    queue._stop_requested = True
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert queue._state == PrintState.IDLE
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_records) == 1, "Stopped paused print should record failure"
    assert fail_records[0][2]["reason"] == "cancelled"


def test_normal_finish_from_paused():
    """value=0 without stop_requested from PAUSED records a normal finish."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    # Normal finish (no stop_requested)
    history_calls.clear()
    events.clear()
    queue._handle_notification({"commandType": 1000, "value": 0})

    assert queue._state == PrintState.IDLE
    finish_records = [c for c in history_calls if c[0] == "finish"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    finish_events = [e for e in events if e[0] == "print_finished"]
    assert len(finish_records) == 1, "Normal finish from paused should record finish"
    assert len(fail_records) == 0, "Normal finish from paused should not record failure"
    assert len(finish_events) == 1, "Normal finish from paused should send finish event"


def test_abort_during_pause():
    """value=8 from PAUSED is a real abort (not pre-print ignore)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    history_calls.clear()
    events.clear()
    timelapse_calls.clear()
    queue._handle_notification({"commandType": 1000, "value": 8})

    assert queue._state == PrintState.IDLE
    fail_records = [c for c in history_calls if c[0] == "fail"]
    fail_events = [e for e in events if e[0] == "print_failed"]
    assert len(fail_records) == 1, "Abort during pause should record failure"
    assert fail_records[0][2]["reason"] == "aborted"
    assert len(fail_events) == 1, "Abort during pause should send fail event"
    assert ("fail",) in timelapse_calls, "Abort during pause should fail timelapse"


def test_ct1001_progress_ignored_during_pause():
    """Progress messages during PAUSED should not emit progress events
    or change state."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    events.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
    })

    assert queue._state == PrintState.PAUSED, "State should remain PAUSED"
    progress_events = [e for e in events if e[0] == "print_progress"]
    assert len(progress_events) == 0, "No progress events during pause"


def test_ct1001_failure_during_pause_is_swallowed():
    """ct=1001 with errorMessage during PAUSED is silently ignored.
    The failure guard checks _state == PRINTING, so PAUSED errors
    don't trigger duplicate failure handling. The printer should send
    ct=1000 value=0 or value=8 for real failures."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    queue._handle_notification({"commandType": 1044, "filePath": "/tmp/cube.gcode"})
    queue._handle_notification({"commandType": 1000, "value": 1})
    queue._handle_notification({"commandType": 1000, "value": 2})
    assert queue._state == PrintState.PAUSED

    events.clear()
    history_calls.clear()
    queue._handle_notification({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
        "errorMessage": "jam",
    })

    assert queue._state == PrintState.PAUSED, "Should remain PAUSED"
    fail_events = [e for e in events if e[0] == "print_failed"]
    fail_records = [c for c in history_calls if c[0] == "fail"]
    assert len(fail_events) == 0, "ct=1001 failure ignored during pause"
    assert len(fail_records) == 0, "ct=1001 failure ignored during pause"


def test_pause_and_resume_guards_reject_invalid_states():
    """value=2 is only valid from PRINTING; value=3 only from PAUSED.
    All other states should be no-ops."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()

    # value=2 from each non-PRINTING state
    for state in (PrintState.IDLE, PrintState.PREPARING, PrintState.PRE_PRINT,
                  PrintState.PAUSED, PrintState.FAILED):
        queue._state = state
        queue._handle_notification({"commandType": 1000, "value": 2})
        assert queue._state == state, f"value=2 from {state.name} should be no-op"

    # value=3 from each non-PAUSED state
    for state in (PrintState.IDLE, PrintState.PREPARING, PrintState.PRE_PRINT,
                  PrintState.PRINTING, PrintState.FAILED):
        queue._state = state
        queue._handle_notification({"commandType": 1000, "value": 3})
        assert queue._state == state, f"value=3 from {state.name} should be no-op"


def test_transition_to_active_blocked_from_paused():
    """_transition_to_active() should not fire from PAUSED state."""
    queue = _queue()
    queue._state = PrintState.PAUSED
    assert queue._transition_to_active({}, progress=50) is False
    assert queue._state == PrintState.PAUSED


def test_forward_to_ha_reports_paused_during_ct1001():
    """_forward_to_ha derives 'paused' status for ct=1001 messages
    while in PAUSED state (separate code path from ct=1000 handler)."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PAUSED

    queue._forward_to_ha({
        "commandType": 1001,
        "progress": 5000,
        "name": "cube.gcode",
    })

    paused_updates = [u for u in ha_updates if u.get("print_status") == "paused"]
    assert len(paused_updates) >= 1, "HA should report 'paused' for ct=1001 during pause"


def test_get_state_during_pause():
    """get_state() output is correct during PAUSED."""
    global ha_updates, history_calls, timelapse_calls, events
    ha_updates, history_calls, timelapse_calls, events = [], [], [], []
    queue = _queue()
    queue._state = PrintState.PAUSED
    queue._last_state_value = 8  # worst case for is_preparing_print

    state = queue.get_state()["print"]
    assert state["print_state"] == "paused"
    assert state["active"] is True
    assert state["in_pre_print_window"] is False
    assert state["preparing"] is False, "is_preparing_print should be False during PAUSED"
    assert state["state_label"] == "preparing_or_aborted"
