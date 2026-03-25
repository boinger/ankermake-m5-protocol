import json
import os
import time
from contextlib import contextmanager
from types import SimpleNamespace

from web.service.timelapse import TimelapseService, _IN_PROGRESS_SUBDIR


class FakeConfigManager:
    def __init__(self, root, enabled=True):
        self.config_root = root
        self._cfg = SimpleNamespace(
            timelapse={
                "enabled": enabled,
                "interval": 5,
                "max_videos": 2,
                "save_persistent": True,
                "light": None,
            }
        )

    @contextmanager
    def open(self):
        yield self._cfg


def test_timelapse_meta_and_video_file_helpers(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    meta_dir = tmp_path / "capture"
    meta_dir.mkdir()
    svc._write_meta(meta_dir, "cube.gcode", 3)

    assert svc._read_meta(meta_dir) == {"filename": "cube.gcode", "frame_count": 3}

    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    video_a.write_bytes(b"a")
    time.sleep(0.01)
    video_b.write_bytes(b"bb")

    videos = svc.list_videos()
    assert [video["filename"] for video in videos] == ["b.mp4", "a.mp4"]
    assert svc.get_video_path("a.mp4") == str(video_a)
    assert svc.get_video_path("../a.mp4") is None
    assert svc.delete_video("a.mp4") is True
    assert not video_a.exists()


def test_timelapse_prunes_old_videos(tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)

    for name in ("old.mp4", "mid.mp4", "new.mp4"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        time.sleep(0.01)

    svc._prune_old_videos()

    remaining = sorted(p.name for p in tmp_path.glob("*.mp4"))
    assert remaining == ["mid.mp4", "new.mp4"]


def test_timelapse_scan_recovers_or_resumes_in_progress(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    base = tmp_path / _IN_PROGRESS_SUBDIR
    base.mkdir()

    young = base / "young_capture"
    young.mkdir()
    (young / "frame_00000.jpg").write_bytes(b"x")
    (young / "frame_00001.jpg").write_bytes(b"y")
    with open(young / ".meta", "w") as fh:
        json.dump({"filename": "young.gcode", "frame_count": 2}, fh)

    old = base / "old_capture"
    old.mkdir()
    (old / "frame_00000.jpg").write_bytes(b"x")
    (old / "frame_00001.jpg").write_bytes(b"y")
    with open(old / ".meta", "w") as fh:
        json.dump({"filename": "old.gcode", "frame_count": 2}, fh)
    stale_time = time.time() - (25 * 3600)
    os.utime(old, (stale_time, stale_time))

    scheduled = []
    assembled = []
    pruned = []

    monkeypatch.setattr(TimelapseService, "_schedule_finalize", lambda self, d, f, c, suffix="": scheduled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": assembled.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: pruned.append(True))

    svc = TimelapseService(cfg, captures_dir=tmp_path)

    assert svc._resume_filename == "young.gcode"
    assert svc._resume_frame_count == 2
    assert scheduled and scheduled[0][1] == "young.gcode"
    assert assembled and assembled[0][1] == "old.gcode" and assembled[0][3] == "_recovered"
    assert pruned == [True]


def test_timelapse_finish_and_fail_paths(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._current_dir = str(tmp_path / "current")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 3
    os.makedirs(svc._current_dir, exist_ok=True)

    finalize_calls = []
    disable_calls = []

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_cancel_pending_resume", lambda self: None)
    monkeypatch.setattr(TimelapseService, "_disable_video_for_timelapse", lambda self: disable_calls.append(True))
    monkeypatch.setattr(TimelapseService, "_assemble_video_from", lambda self, d, f, c, suffix="": finalize_calls.append((d, f, c, suffix)))
    monkeypatch.setattr(TimelapseService, "_prune_old_videos", lambda self: finalize_calls.append(("prune",)))
    monkeypatch.setattr(TimelapseService, "_cleanup_dir", lambda self, d: finalize_calls.append(("cleanup", d)))

    svc.finish_capture(final=True)

    assert ("prune",) in finalize_calls
    assert any(call[:4] == (str(tmp_path / "current"), "cube.gcode", 3, "") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]

    svc._current_dir = str(tmp_path / "failed")
    svc._current_filename = "failed.gcode"
    svc._frame_count = 2
    os.makedirs(svc._current_dir, exist_ok=True)
    finalize_calls.clear()
    disable_calls.clear()

    svc.fail_capture()

    assert any(call[:4] == (str(tmp_path / "failed"), "failed.gcode", 2, "_partial") for call in finalize_calls if len(call) == 4)
    assert disable_calls == [True]


def test_timelapse_start_capture_resumes_pending_session(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    resume_dir = str(tmp_path / _IN_PROGRESS_SUBDIR / "resume")
    os.makedirs(resume_dir, exist_ok=True)
    svc._resume_dir = resume_dir
    svc._resume_filename = "cube.gcode"
    svc._resume_frame_count = 4

    calls = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target

        def start(self):
            calls.append("thread-start")

    monkeypatch.setattr("web.service.timelapse.shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(TimelapseService, "_stop_capture_thread", lambda self: calls.append("stop-thread"))
    monkeypatch.setattr(TimelapseService, "_cancel_finalize_timer", lambda self: calls.append("cancel-finalize"))
    monkeypatch.setattr(TimelapseService, "_cancel_pending_resume", lambda self: calls.append("cancel-pending"))
    monkeypatch.setattr(TimelapseService, "_enable_video_for_timelapse", lambda self: calls.append("enable-video"))
    monkeypatch.setattr("web.service.timelapse.threading.Thread", FakeThread)

    svc.start_capture("cube.gcode")

    assert svc._current_dir == resume_dir
    assert svc._current_filename == "cube.gcode"
    assert svc._frame_count == 4
    assert svc._resume_dir is None
    assert calls == ["stop-thread", "cancel-finalize", "enable-video", "thread-start"]


def test_timelapse_take_snapshot_retries_and_restores_light(monkeypatch, tmp_path):
    cfg = FakeConfigManager(tmp_path)
    svc = TimelapseService(cfg, captures_dir=tmp_path)
    svc._light_mode = "snapshot"
    svc._current_dir = str(tmp_path / "capture")
    svc._current_filename = "cube.gcode"
    svc._frame_count = 0
    os.makedirs(svc._current_dir, exist_ok=True)

    light_calls = []
    videoqueue = SimpleNamespace(saved_light_state=False, api_light_state=lambda state: light_calls.append(state))
    old_svc = getattr(__import__("web").app, "svc")
    old_api_key = __import__("web").app.config.get("api_key")
    __import__("web").app.svc = SimpleNamespace(svcs={"videoqueue": videoqueue})
    __import__("web").app.config["api_key"] = "secret-key"

    runs = []

    def fake_run(cmd, stdout=None, stderr=None, timeout=None):
        runs.append(cmd)
        frame_path = cmd[-1]
        if len(runs) == 2:
            with open(frame_path, "wb") as fh:
                fh.write(b"jpg")
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=1)

    try:
        monkeypatch.setattr(TimelapseService, "_await_video_frame", lambda self: True)
        monkeypatch.setattr("web.service.timelapse.subprocess.run", fake_run)
        monkeypatch.setattr("web.service.timelapse.time.sleep", lambda seconds: None)
        svc._take_snapshot()
    finally:
        __import__("web").app.svc = old_svc
        __import__("web").app.config["api_key"] = old_api_key

    assert len(runs) == 2
    assert any("apikey=secret-key" in part for part in runs[0] if isinstance(part, str))
    assert light_calls == [True, False]
    assert svc._frame_count == 1
    assert os.path.exists(os.path.join(svc._current_dir, "frame_00000.jpg"))


def test_capture_thread_crash_clears_ref(tmp_path):
    """If _capture_loop() crashes from an uncaught exception, the
    try/finally should clear _capture_thread so start_capture() knows
    the thread is dead."""
    import threading
    from types import SimpleNamespace

    class CaptureLoopCrash(BaseException):
        """Uncatchable by except Exception — simulates a fatal crash."""

    config_mgr = SimpleNamespace(config_root=str(tmp_path))
    svc = TimelapseService(config_mgr, captures_dir=str(tmp_path))
    svc._interval = 0.01

    call_count = [0]

    def crashing_snapshot():
        call_count[0] += 1
        if call_count[0] >= 2:
            raise CaptureLoopCrash("Simulated fatal crash")

    svc._take_snapshot = crashing_snapshot
    svc._capture_thread = threading.Thread(target=svc._capture_loop, daemon=True)
    svc._capture_thread.start()
    svc._capture_thread.join(timeout=2)

    assert svc._capture_thread is None, "_capture_thread should be None after crash"
