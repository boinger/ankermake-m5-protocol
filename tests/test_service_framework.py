from contextlib import contextmanager
from queue import Queue
from threading import Timer
from types import SimpleNamespace

import pytest

from web.lib.service import RunState, ServiceError, ServiceManager, ServiceStoppedError


class FakeManagedService:
    def __init__(self, *, wanted=False, state=RunState.Stopped, ready_error=None, video_enabled=False):
        self.wanted = wanted
        self.state = state
        self.running = True
        self.video_enabled = video_enabled
        self.ready_error = ready_error
        self.start_calls = 0
        self.stop_calls = 0
        self.await_ready_calls = 0
        self.await_stopped_calls = 0
        self.shutdown_calls = 0

    def start(self):
        self.start_calls += 1
        self.wanted = True

    def stop(self):
        self.stop_calls += 1
        self.wanted = False

    def await_ready(self):
        self.await_ready_calls += 1
        if self.ready_error:
            raise self.ready_error
        self.state = RunState.Running
        return True

    def await_stopped(self):
        self.await_stopped_calls += 1
        self.state = RunState.Stopped
        return True

    def shutdown(self):
        self.shutdown_calls += 1

    @contextmanager
    def tap(self, handler):
        handler({"status": "frame"})
        Timer(0.01, lambda: setattr(self, "state", RunState.Stopped)).start()
        yield self


def test_service_manager_register_get_put_and_unregister():
    manager = ServiceManager()
    svc = FakeManagedService()

    manager.register("mqttqueue", svc)
    with pytest.raises(KeyError):
        manager.register("mqttqueue", svc)

    fetched = manager.get("mqttqueue")
    assert fetched is svc
    assert manager.refs["mqttqueue"] == 1
    assert svc.start_calls == 1
    assert svc.await_ready_calls == 1

    manager.put("mqttqueue")
    assert manager.refs["mqttqueue"] == 0
    assert svc.stop_calls == 1

    manager.unregister("mqttqueue")
    assert "mqttqueue" not in manager


def test_service_manager_get_rolls_back_on_ready_failure_and_video_put_keeps_running():
    manager = ServiceManager()
    broken = FakeManagedService(ready_error=ServiceError("boom"))
    video = FakeManagedService(state=RunState.Running, video_enabled=True)
    video.persistent = True  # video_enabled=True keeps service alive (via persistent flag)
    manager.register("broken", broken)
    manager.register("videoqueue", video)

    with pytest.raises(ServiceError, match="boom"):
        manager.get("broken")
    assert manager.refs["broken"] == 0
    assert broken.stop_calls == 1

    manager.refs["videoqueue"] = 1
    manager.put("videoqueue")
    assert manager.refs["videoqueue"] == 0
    assert video.stop_calls == 0


def test_service_manager_restart_all_stream_and_atexit():
    manager = ServiceManager()
    wanted = FakeManagedService(wanted=True, state=RunState.Running)
    wanted_stops = FakeManagedService(wanted=True, state=RunState.Running, ready_error=ServiceStoppedError("stopped"))
    idle = FakeManagedService(wanted=False, state=RunState.Stopped)

    manager.register("wanted", wanted)
    manager.register("wanted_stops", wanted_stops)
    manager.register("idle", idle)

    manager.restart_all()
    streamed = list(manager.stream("wanted"))
    manager.atexit()

    assert wanted.stop_calls >= 1
    assert wanted.start_calls == 2
    assert wanted.await_ready_calls == 2
    assert wanted_stops.start_calls == 1
    assert wanted_stops.await_ready_calls == 1
    assert idle.start_calls == 0
    assert streamed == [{"status": "frame"}]
    assert wanted.shutdown_calls == 1
    assert wanted_stops.shutdown_calls == 1
    assert idle.shutdown_calls == 1


def test_service_stream_bounded_queue_drops_oldest_items():
    q = Queue(maxsize=2)

    ServiceManager._enqueue_stream_item(q, "one")
    ServiceManager._enqueue_stream_item(q, "two")
    ServiceManager._enqueue_stream_item(q, "three")

    assert q.get_nowait() == "two"
    assert q.get_nowait() == "three"
