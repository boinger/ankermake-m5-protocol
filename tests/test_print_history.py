import os

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


def test_history_clear_and_fallback_finish_latest_active(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    history.record_start("one.gcode", task_id="1")
    history.record_start("two.gcode", task_id="2")
    history.record_finish()

    entries = history.get_history(limit=10)
    assert entries[0]["filename"] == "two.gcode"
    assert entries[0]["status"] == "finished"

    history.clear()
    assert history.get_count() == 0


def test_archive_upload_and_reprint_flags(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    archive_info = history.archive_upload(
        "cube.gcode",
        (
            b"; thumbnail begin 32x32 10\n"
            b"; iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5ZQAAAAASUVORK5CYII=\n"
            b"; thumbnail end\n"
            b"G28\nM104 S200\n"
        ),
    )
    row_id = history.record_start(
        "cube.gcode",
        task_id="task-archive",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )

    entry = history.get_entry(row_id)

    assert entry["archive_available"] is True
    assert entry["can_reprint"] is True
    assert entry["thumbnail_available"] is True
    assert history.get_archive_path(row_id) is not None
    assert history.get_thumbnail_path(row_id) is not None


def test_history_clear_removes_archived_gcode_files(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    archive_info = history.archive_upload("cube.gcode", b"G28\n")
    row_id = history.record_start(
        "cube.gcode",
        archive_relpath=archive_info["archive_relpath"],
        archive_size=archive_info["archive_size"],
    )
    assert history.get_archive_path(row_id) is not None

    history.clear()

    assert history.get_archive_path(row_id) is None


def test_history_preview_url_marks_entry_thumbnail_available(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    row_id = history.record_start(
        "usb-file.gcode",
        preview_url="https://example.test/preview.png",
    )

    entry = history.get_entry(row_id)

    assert entry["thumbnail_available"] is True
    assert entry["preview_url"] == "https://example.test/preview.png"


def test_delete_entries_removes_selected_rows_and_unreferenced_archives(tmp_path):
    history = PrintHistory(db_path=tmp_path / "history.db")

    first_archive = history.archive_upload("one.gcode", b"G28\n")
    first_id = history.record_start(
        "one.gcode",
        archive_relpath=first_archive["archive_relpath"],
        archive_size=first_archive["archive_size"],
    )
    second_archive = history.archive_upload("two.gcode", b"G28\n")
    second_id = history.record_start(
        "two.gcode",
        archive_relpath=second_archive["archive_relpath"],
        archive_size=second_archive["archive_size"],
    )

    first_archive_path = os.path.join(tmp_path, "gcode_archive", first_archive["archive_relpath"])
    second_archive_path = os.path.join(tmp_path, "gcode_archive", second_archive["archive_relpath"])

    deleted = history.delete_entries([first_id])

    assert deleted == 1
    assert history.get_entry(first_id) is None
    assert history.get_entry(second_id) is not None
    assert first_archive_path is not None and not os.path.exists(first_archive_path)
    assert second_archive_path is not None and os.path.exists(second_archive_path)
