from web.service.history import PrintHistory


def test_record_start_skips_placeholder_names(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    assert history.record_start("unknown") is None
    assert history.get_count() == 0


def test_record_start_reuses_same_task_id_and_interrupts_orphans(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    first_id = history.record_start("part-a.gcode", task_id="task-1")
    resumed_id = history.record_start("part-a.gcode", task_id="task-1")
    history.record_start("part-b.gcode", task_id="task-2")

    entries = history.get_history(limit=10)

    assert resumed_id == first_id
    assert entries[0]["filename"] == "part-b.gcode"
    assert entries[0]["status"] == "started"
    assert entries[1]["filename"] == "part-a.gcode"
    assert entries[1]["status"] == "interrupted"
    assert entries[1]["duration_sec"] >= 0


def test_record_finish_and_fail_update_active_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    history.record_start("success.gcode", task_id="task-success")
    history.record_finish(task_id="task-success", progress=100)

    history.record_start("failed.gcode", task_id="task-fail")
    history.record_fail(task_id="task-fail", reason="jam")

    entries = history.get_history(limit=10)

    assert entries[0]["filename"] == "failed.gcode"
    assert entries[0]["status"] == "failed"
    assert entries[0]["failure_reason"] == "jam"
    assert entries[1]["filename"] == "success.gcode"
    assert entries[1]["status"] == "finished"
    assert entries[1]["progress"] == 100


def test_history_prunes_to_max_entries(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db", max_entries=2)

    history.record_start("one.gcode", task_id="1")
    history.record_start("two.gcode", task_id="2")
    history.record_start("three.gcode", task_id="3")

    entries = history.get_history(limit=10)

    assert history.get_count() == 2
    assert [entry["filename"] for entry in entries] == ["three.gcode", "two.gcode"]
