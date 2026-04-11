"""
This module is designed to implement a Flask web server for video
streaming and handling other functionalities of AnkerMake M5.
It also implements various services, routes and functions including.

Methods:
    - startup(): Registers required services on server start

Routes:
    - /ws/mqtt: Handles receiving and sending messages on the 'mqttqueue' stream service through websocket
    - /ws/pppp-state: Provides the status of the 'pppp' stream service through websocket
    - /ws/video: Handles receiving and sending messages on the 'videoqueue' stream service through websocket
    - /ws/ctrl: Handles controlling of light and video quality through websocket
    - /video: Handles the video streaming/downloading feature in the Flask app
    - /: Renders the html template for the root route, which is the homepage of the Flask app
    - /api/version: Returns the version details of api and server as dictionary
    - /api/ankerctl/config/upload: Handles the uploading of configuration file \
        to Flask server and returns a HTML redirect response
    - /api/ankerctl/config/login: Performs email/password login and saves configuration
    - /api/ankerctl/server/reload: Reloads the Flask server and returns a HTML redirect response
    - /api/files/local: Handles the uploading of files to Flask server and returns a dictionary containing file details

Functions:
    - webserver(config, host, port, **kwargs): Starts the Flask webserver

Services:
    - util: Houses utility services for use in the web module
    - config: Handles configuration manipulation for ankerctl
"""
import json
import logging
import math
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager

log = logging.getLogger("web")


class _AccessLogNoiseFilter(logging.Filter):
    _IGNORED_SUBSTRINGS = (
        '"GET /static/',
        '"GET /favicon.ico',
        '"GET /api/console/logs',
        '"GET /api/printer/alerts',
        '"GET /api/printer/runtime-state',
        '"GET /api/camera/frame',
    )

    def filter(self, record):
        message = record.getMessage()
        return not any(fragment in message for fragment in self._IGNORED_SUBSTRINGS)


def _configure_access_log_noise():
    werkzeug_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, _AccessLogNoiseFilter) for f in werkzeug_log.filters):
        werkzeug_log.addFilter(_AccessLogNoiseFilter())


class _ConsoleLogBuffer:
    def __init__(self, max_lines=2000):
        self._entries = deque(maxlen=max_lines)
        self._next_id = 1
        self._lock = threading.Lock()

    @property
    def max_lines(self):
        return self._entries.maxlen or 0

    def append(self, text):
        text = str(text or "").rstrip("\r\n")
        if not text:
            return None
        with self._lock:
            entry = {"id": self._next_id, "text": text}
            self._entries.append(entry)
            self._next_id += 1
            return entry["id"]

    def snapshot(self, *, limit=200, after_id=None):
        limit = max(1, min(int(limit), self.max_lines or 1))
        with self._lock:
            entries = list(self._entries)

        first_id = entries[0]["id"] if entries else 0
        last_id = entries[-1]["id"] if entries else 0
        truncated = False

        if after_id is None:
            selected = entries[-limit:]
        else:
            try:
                after_id = int(after_id)
            except (TypeError, ValueError):
                after_id = 0

            if entries and after_id < first_id - 1:
                truncated = True

            selected = [entry for entry in entries if entry["id"] > after_id]
            if len(selected) > limit:
                truncated = True
                selected = selected[-limit:]

        return {
            "entries": [dict(entry) for entry in selected],
            "first_id": first_id,
            "last_id": last_id,
            "next_after": last_id,
            "truncated": truncated,
            "max_lines": self.max_lines,
        }


class _ConsoleLogFormatter(logging.Formatter):
    _MARKS = {
        logging.CRITICAL: "!",
        logging.ERROR: "E",
        logging.WARNING: "W",
        logging.INFO: "*",
        logging.DEBUG: "D",
    }

    def format(self, record):
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        mark = self._MARKS.get(record.levelno, "*")
        lines = str(message or "").splitlines() or [""]
        return "\n".join(([f"[{mark}] {lines[0]}"] + lines[1:]))


class _ConsoleLogBufferHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer
        self._ankerctl_console_buffer_handler = True

    def emit(self, record):
        try:
            formatted = self.format(record)
            for line in str(formatted).splitlines():
                self.buffer.append(line)
        except Exception:
            self.handleError(record)


class _PrinterAlertBuffer:
    def __init__(self, max_entries=100):
        self._entries = deque(maxlen=max_entries)
        self._next_id = 1
        self._recent_keys = {}
        self._lock = threading.Lock()

    @property
    def max_entries(self):
        return self._entries.maxlen or 0

    def append(
        self,
        *,
        printer_index,
        printer_name,
        alert_type,
        title,
        message,
        level="warning",
        cooldown_sec=30,
    ):
        message = str(message or "").strip()
        if not message:
            return None

        title = str(title or "").strip() or message
        level = str(level or "warning").strip() or "warning"
        alert_key = f"{printer_index}:{alert_type}:{title}:{message}"
        now = time.monotonic()

        with self._lock:
            if cooldown_sec:
                last_seen = self._recent_keys.get(alert_key)
                if last_seen is not None and now - last_seen < float(cooldown_sec):
                    return None
            self._recent_keys[alert_key] = now
            stale_before = now - max(float(cooldown_sec or 0) * 4, 60.0)
            self._recent_keys = {
                key: seen_at
                for key, seen_at in self._recent_keys.items()
                if seen_at >= stale_before
            }

            entry = {
                "id": self._next_id,
                "created_at": time.time(),
                "printer_index": printer_index,
                "printer_name": printer_name,
                "type": alert_type,
                "title": title,
                "message": message,
                "level": level,
            }
            self._entries.append(entry)
            self._next_id += 1
            return entry["id"]

    def snapshot(self, *, limit=50, after_id=None):
        limit = max(1, min(int(limit), self.max_entries or 1))
        with self._lock:
            entries = list(self._entries)

        first_id = entries[0]["id"] if entries else 0
        last_id = entries[-1]["id"] if entries else 0
        truncated = False

        if after_id is None:
            selected = entries[-limit:]
        else:
            try:
                after_id = int(after_id)
            except (TypeError, ValueError):
                after_id = 0

            if entries and after_id < first_id - 1:
                truncated = True

            selected = [entry for entry in entries if entry["id"] > after_id]
            if len(selected) > limit:
                truncated = True
                selected = selected[-limit:]

        return {
            "entries": [dict(entry) for entry in selected],
            "first_id": first_id,
            "last_id": last_id,
            "next_after": last_id,
            "truncated": truncated,
            "max_entries": self.max_entries,
        }


from secrets import token_urlsafe as token
from flask import Flask, flash, request, render_template, Response, session, url_for, jsonify, has_request_context
from flask_sock import Sock
from simple_websocket.errors import ConnectionClosed
from user_agents import parse as user_agent_parse

from libflagship import ROOT_DIR
import libflagship.httpapi
import libflagship.logincache
from libflagship.notifications import AppriseClient

from web.lib.service import ServiceManager, RunState, ServiceStoppedError

import web.config
import web.camera
import web.platform
import web.util

import cli.util
import cli.config
import cli.countrycodes
import cli.mqtt
from cli.model import (
    UPLOAD_RATE_MBPS_CHOICES,
    default_apprise_config,
    default_notifications_config,
    merge_dict_defaults,
)


app = Flask(__name__, root_path=ROOT_DIR, static_folder="static", template_folder="static")
app.config.from_prefixed_env()
# secret_key is required for session cookies and flash() to function.
# Run after from_prefixed_env() so env var wins; fall back to a random token
# if FLASK_SECRET_KEY is absent or set to an empty string.
if not app.secret_key:
    app.secret_key = token(24)
app.svc = ServiceManager()
app.filament_swap_lock = threading.Lock()
app.filament_swap_state = None
app.pppp_probe_lock = threading.Lock()


def _default_pppp_probe_state():
    return {
        "result": None,          # None=never probed, True=reachable, False=unreachable
        "last_time": 0.0,        # time.time() of last completed probe
        "fail_count": 0,         # consecutive failures since last success
        "thread": None,          # current probe Thread or None
        "client_count": 0,       # active WS clients watching pppp-state
    }


app.pppp_probe = {
    "per_printer": {},
}


def _env_int(name, default, min_value=1, env=None):
    env = os.environ if env is None else env
    raw = env.get(name)
    if raw in (None, ""):
        return default

    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("Ignoring invalid integer value for %s: %r", name, raw)
        return default

    if value < min_value:
        log.warning("Ignoring %s=%r because it is smaller than %d", name, raw, min_value)
        return default

    return value


def _ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found

    cached = getattr(_ffmpeg_path, "_cached", None)
    if cached and os.path.isfile(cached):
        return cached

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        packages_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages_dir):
            for root, _, files in os.walk(packages_dir):
                if "ffmpeg.exe" in files:
                    candidate = os.path.join(root, "ffmpeg.exe")
                    _ffmpeg_path._cached = candidate
                    return candidate

    return None


def _ffmpeg_available():
    return _ffmpeg_path() is not None


SNAPSHOT_FRAME_WAIT_SEC = 3.0
SNAPSHOT_FRAME_MAX_AGE_SEC = 2.0
SNAPSHOT_FFMPEG_TIMEOUT_SEC = 30
VIDEO_STREAM_QUEUE_MAX = 30


def _video_has_recent_frame(vq, wait_sec=SNAPSHOT_FRAME_WAIT_SEC, max_age_sec=SNAPSHOT_FRAME_MAX_AGE_SEC):
    if not hasattr(vq, "last_frame_at"):
        return True

    deadline = time.monotonic() + wait_sec
    while True:
        last_frame_at = getattr(vq, "last_frame_at", None)
        if last_frame_at is not None and time.monotonic() - last_frame_at <= max_age_sec:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _active_printer(cfg=None, printer_index=None):
    if cfg is None:
        config = app.config.get("config")
        if not config:
            return None
        with config.open() as opened_cfg:
            return _active_printer(opened_cfg, printer_index=printer_index)

    printers = getattr(cfg, "printers", None) or []
    if printer_index is None:
        printer_index = app.config.get("printer_index", 0)
    if printer_index < 0 or printer_index >= len(printers):
        return None
    return printers[printer_index]


def _resolve_camera_settings(cfg=None, printer_index=None):
    if cfg is None:
        config = app.config.get("config")
        if not config:
            return web.camera.resolve_camera_settings(None, printer_index or 0)
        with config.open() as opened_cfg:
            return _resolve_camera_settings(opened_cfg, printer_index=printer_index)

    if printer_index is None:
        printer_index = app.config.get("printer_index", 0)
    return web.camera.resolve_camera_settings(cfg, printer_index=printer_index)


def _camera_feature_available(cfg=None, printer_index=None):
    camera_settings = _resolve_camera_settings(cfg, printer_index=printer_index)
    return bool(camera_settings.get("feature_available"))


def _requested_printer_index(default=None):
    fallback = app.config.get("printer_index", 0) if default is None else default
    if not has_request_context():
        return fallback
    raw = request.args.get("printer_index", default=fallback, type=int)
    try:
        printer_index = int(raw)
    except (TypeError, ValueError):
        return fallback

    config = app.config.get("config")
    if not config:
        return printer_index

    try:
        with config.open() as cfg:
            printers = getattr(cfg, "printers", []) or []
            if 0 <= printer_index < len(printers):
                return printer_index
    except Exception:
        pass
    return fallback


def _printer_video_supported(cfg=None, printer_index=None):
    active_index = app.config.get("printer_index", 0)
    if cfg is None and (printer_index is None or printer_index == active_index):
        override = app.config.get("video_supported")
        if override is not None:
            return bool(override)
    camera_settings = _resolve_camera_settings(cfg, printer_index=printer_index)
    return bool(camera_settings.get("printer_supported"))


def _get_pppp_probe_state(printer_index=None, create=True):
    printer_index = 0 if printer_index is None else int(printer_index)
    probe = getattr(app, "pppp_probe", None)
    if not isinstance(probe, dict):
        probe = {}
        app.pppp_probe = probe

    # Backward compatibility for older flat probe dicts used in lightweight tests.
    if "per_printer" not in probe:
        if printer_index == 0:
            for key, value in _default_pppp_probe_state().items():
                probe.setdefault(key, value)
            return probe
        per_printer = {0: probe}
        app.pppp_probe = {"per_printer": per_printer}
        probe = app.pppp_probe

    per_printer = probe.setdefault("per_printer", {})
    if printer_index not in per_printer and create:
        per_printer[printer_index] = _default_pppp_probe_state()
    return per_printer.get(printer_index)


def _build_runtime_state_payload(mqtt, cfg=None, printer_index=None):
    state = mqtt.get_state() if mqtt else {}
    payload = dict(state)
    payload["camera"] = web.camera.runtime_camera_state(
        _resolve_camera_settings(cfg, printer_index=printer_index)
    )
    return payload


def _build_windows_launcher_bat(install_dir):
    install_dir = str(install_dir or "").strip()
    if not install_dir:
        raise ValueError("Install directory is required.")
    if '"' in install_dir:
        raise ValueError('Install directory cannot contain double quotes.')

    escaped_dir = install_dir.replace("%", "%%")
    lines = [
        "@echo off",
        "setlocal",
        f'set "ANKERCTL_DIR={escaped_dir}"',
        'cd /d "%ANKERCTL_DIR%" || (',
        '    echo Could not open the Ankerctl folder:',
        '    echo %ANKERCTL_DIR%',
        "    pause",
        "    exit /b 1",
        ")",
        "echo Starting ankerctl web server...",
        "where py >nul 2>&1",
        "if %errorlevel%==0 (",
        "    py .\\ankerctl.py webserver run",
        ") else (",
        "    python .\\ankerctl.py webserver run",
        ")",
        "echo.",
        "echo ankerctl exited.",
        "pause",
    ]
    return "\r\n".join(lines) + "\r\n"


def _configure_request_limits(flask_app, env=None):
    # Keep large GCode uploads configurable, but bound multipart metadata more
    # tightly. These form limits apply to multipart parsing, not the file size.
    max_upload_mb = _env_int("UPLOAD_MAX_MB", 2048, env=env)
    max_form_memory_kb = _env_int("UPLOAD_MAX_FORM_MEMORY_KB", 512, env=env)
    max_form_parts = _env_int("UPLOAD_MAX_FORM_PARTS", 20, env=env)

    flask_app.config["MAX_CONTENT_LENGTH"] = max_upload_mb * 1024 * 1024
    flask_app.config["MAX_FORM_MEMORY_SIZE"] = max_form_memory_kb * 1024
    flask_app.config["MAX_FORM_PARTS"] = max_form_parts


_configure_request_limits(app)


def _get_console_log_buffer():
    buffer = getattr(app, "console_log_buffer", None)
    if buffer is None:
        max_lines = _env_int("ANKERCTL_CONSOLE_BUFFER_LINES", 2000, min_value=100)
        buffer = _ConsoleLogBuffer(max_lines=max_lines)
        app.console_log_buffer = buffer

    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "_ankerctl_console_buffer_handler", False):
            if getattr(handler, "buffer", None) is not buffer:
                buffer = handler.buffer
                app.console_log_buffer = buffer
            return buffer

    if not root.handlers:
        return buffer

    handler = _ConsoleLogBufferHandler(buffer)
    handler.setFormatter(_ConsoleLogFormatter())
    root.addHandler(handler)
    return buffer


def _get_printer_alert_buffer():
    buffer = getattr(app, "printer_alert_buffer", None)
    if buffer is None:
        max_entries = _env_int("ANKERCTL_PRINTER_ALERT_BUFFER_SIZE", 100, min_value=10)
        buffer = _PrinterAlertBuffer(max_entries=max_entries)
        app.printer_alert_buffer = buffer
    return buffer


def _record_printer_alert(
    *,
    printer_index,
    printer_name,
    alert_type,
    title,
    message,
    level="warning",
    cooldown_sec=30,
):
    buffer = _get_printer_alert_buffer()
    return buffer.append(
        printer_index=printer_index,
        printer_name=printer_name,
        alert_type=alert_type,
        title=title,
        message=message,
        level=level,
        cooldown_sec=cooldown_sec,
    )

# Session cookie security
app.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.record_printer_alert = _record_printer_alert

# Resolve log directory once: honour env var, fall back to None on bare metal
_log_dir = os.getenv("ANKERCTL_LOG_DIR") or ("/logs" if os.path.isdir("/logs") else None)

sock = Sock(app)

PRINTERS_WITHOUT_CAMERA = sorted(web.camera.PRINTERS_WITHOUT_CAMERA)

# Devices that must never be controlled — not 3D printers (e.g. UV printers).
# When the active printer matches one of these model codes, all services are
# suppressed and printer-control API endpoints return 503.
UNSUPPORTED_PRINTERS = ["V8260"]

MQTT_SERVICE_PREFIX = "mqttqueue:"
LEGACY_MQTT_SERVICE_NAME = "mqttqueue"
VIDEO_SERVICE_PREFIX = "videoqueue:"
LEGACY_VIDEO_SERVICE_NAME = "videoqueue"
PPPP_SERVICE_PREFIX = "pppp:"
LEGACY_PPPP_SERVICE_NAME = "pppp"


def mqtt_service_name(printer_index=None):
    if printer_index is None:
        printer_index = app.config.get("printer_index", 0)
    if printer_index is None:
        printer_index = 0
    return f"{MQTT_SERVICE_PREFIX}{printer_index}"


def _mqtt_service_candidates(printer_index=None):
    current = mqtt_service_name(printer_index)
    candidates = [current]
    use_legacy_fallback = printer_index in (None, 0)

    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        if (
            use_legacy_fallback
            and LEGACY_MQTT_SERVICE_NAME in svcs
            and LEGACY_MQTT_SERVICE_NAME not in candidates
        ):
            candidates.insert(0, LEGACY_MQTT_SERVICE_NAME)
        return candidates

    # Minimal test doubles often expose a single legacy mqttqueue service.
    if use_legacy_fallback and current == f"{MQTT_SERVICE_PREFIX}0":
        candidates.insert(0, LEGACY_MQTT_SERVICE_NAME)
    return candidates


def video_service_name(printer_index=None):
    if printer_index is None:
        printer_index = app.config.get("printer_index", 0)
    if printer_index is None:
        printer_index = 0
    return f"{VIDEO_SERVICE_PREFIX}{printer_index}"


def _video_service_candidates(printer_index=None):
    current = video_service_name(printer_index)
    candidates = [current]
    use_legacy_fallback = printer_index in (None, 0)

    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        if (
            use_legacy_fallback
            and LEGACY_VIDEO_SERVICE_NAME in svcs
            and LEGACY_VIDEO_SERVICE_NAME not in candidates
        ):
            candidates.append(LEGACY_VIDEO_SERVICE_NAME)
        return candidates

    if use_legacy_fallback and current == f"{VIDEO_SERVICE_PREFIX}0":
        candidates.append(LEGACY_VIDEO_SERVICE_NAME)
    return candidates


def pppp_service_name(printer_index=None):
    if printer_index is None:
        printer_index = app.config.get("printer_index", 0)
    if printer_index is None:
        printer_index = 0
    return f"{PPPP_SERVICE_PREFIX}{printer_index}"


def _pppp_service_candidates(printer_index=None):
    current = pppp_service_name(printer_index)
    candidates = [current]
    use_legacy_fallback = printer_index in (None, 0)

    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        if (
            use_legacy_fallback
            and LEGACY_PPPP_SERVICE_NAME in svcs
            and LEGACY_PPPP_SERVICE_NAME not in candidates
        ):
            candidates.append(LEGACY_PPPP_SERVICE_NAME)
        return candidates

    if use_legacy_fallback and current == f"{PPPP_SERVICE_PREFIX}0":
        candidates.append(LEGACY_PPPP_SERVICE_NAME)
    return candidates


@contextmanager
def borrow_mqtt(printer_index=None):
    last_error = None
    for name in _mqtt_service_candidates(printer_index):
        try:
            with app.svc.borrow(name) as mqtt:
                yield mqtt
                return
        except (AssertionError, KeyError, AttributeError) as err:
            last_error = err
            continue
    if last_error is not None:
        raise last_error
    # Unreachable: _mqtt_service_candidates always returns at least one candidate.
    raise RuntimeError("No MQTT service candidates available")


def stream_mqtt(printer_index=None):
    last_error = None
    for name in _mqtt_service_candidates(printer_index):
        try:
            return app.svc.stream(name)
        except (AssertionError, KeyError, AttributeError) as err:
            last_error = err
            continue
    if last_error is not None:
        raise last_error
    return iter(())


def get_mqtt_service(printer_index=None):
    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        for name in _mqtt_service_candidates(printer_index):
            if name in svcs:
                return svcs.get(name)
        return None
    return getattr(app.svc, "_mqtt", None)


@contextmanager
def borrow_videoqueue(printer_index=None):
    last_error = None
    for name in _video_service_candidates(printer_index):
        try:
            with app.svc.borrow(name) as videoqueue:
                if videoqueue is None:
                    continue
                yield videoqueue
                return
        except (AssertionError, KeyError, AttributeError) as err:
            last_error = err
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("No videoqueue service candidates available")


def stream_videoqueue(printer_index=None, maxsize=0):
    ordered_names = [resolve_video_service_name(printer_index)]
    ordered_names.extend(
        name for name in _video_service_candidates(printer_index)
        if name not in ordered_names
    )
    last_error = None
    for name in ordered_names:
        try:
            return app.svc.stream(name, maxsize=maxsize)
        except (AssertionError, KeyError, AttributeError) as err:
            last_error = err
            continue
    if last_error is not None:
        raise last_error
    return iter(())


def get_video_service(printer_index=None):
    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        for name in _video_service_candidates(printer_index):
            if name in svcs:
                return svcs.get(name)
        return None
    return getattr(app.svc, "_videoqueue", None)


def get_pppp_service(printer_index=None):
    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        for name in _pppp_service_candidates(printer_index):
            if name in svcs:
                return svcs.get(name)
        return None
    return getattr(app.svc, "_pppp", None)


def resolve_video_service_name(printer_index=None):
    svcs = getattr(app.svc, "svcs", None)
    candidates = _video_service_candidates(printer_index)
    if isinstance(svcs, dict):
        for name in candidates:
            if name in svcs:
                return name
    if printer_index in (None, 0):
        return LEGACY_VIDEO_SERVICE_NAME
    return candidates[0]


def resolve_pppp_service_name(printer_index=None):
    svcs = getattr(app.svc, "svcs", None)
    candidates = _pppp_service_candidates(printer_index)
    if isinstance(svcs, dict):
        for name in candidates:
            if name in svcs:
                return name
    if printer_index in (None, 0):
        return LEGACY_PPPP_SERVICE_NAME
    return candidates[0]


def iter_mqtt_services():
    svcs = getattr(app.svc, "svcs", None)
    if isinstance(svcs, dict):
        for name, svc in svcs.items():
            if name == LEGACY_MQTT_SERVICE_NAME or name.startswith(MQTT_SERVICE_PREFIX):
                yield name, svc
        return

    # Fall back to a single borrowed service for lightweight test doubles.
    with borrow_mqtt() as mqtt:
        if mqtt is not None:
            yield _mqtt_service_candidates()[0], mqtt


def _stop_switchable_services():
    vq = get_video_service()
    if vq:
        vq.set_video_enabled(False)
        vq.stop()
        try:
            vq.await_stopped()
        except Exception as exc:
            log.debug(f"VideoQueue stop wait failed: {exc}")

    pppp = get_pppp_service()
    if pppp:
        pppp.stop()
        try:
            pppp.await_stopped()
        except Exception as exc:
            log.debug(f"PPPPService stop wait failed: {exc}")
        try:
            for name in _pppp_service_candidates():
                if getattr(app.svc, "svcs", None) and name in app.svc.svcs:
                    app.svc.unregister(name)
                    break
        except Exception as exc:
            log.debug(f"PPPPService unregister failed: {exc}")


def _deep_update(base, updates):
    if not isinstance(base, dict):
        base = {}
    if not isinstance(updates, dict):
        return base
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_update(base.get(key), value)
        else:
            base[key] = value
    return base


def _resolve_notifications(cfg):
    return merge_dict_defaults(getattr(cfg, "notifications", None), default_notifications_config())


def _resolve_apprise(cfg):
    notifications = _resolve_notifications(cfg)
    apprise = merge_dict_defaults(notifications.get("apprise"), default_apprise_config())
    progress = apprise.get("progress")
    if isinstance(progress, dict):
        progress.pop("max_value", None)
    return apprise


def _resolve_filament_service_settings(cfg):
    return merge_dict_defaults(
        getattr(cfg, "filament_service", None),
        cli.model.default_filament_service_config(),
    )


FILAMENT_SERVICE_DEFAULT_LENGTH_MM = 40.0
FILAMENT_SERVICE_MAX_LENGTH_MM = 300.0
FILAMENT_SERVICE_FEEDRATE_MM_MIN = 240
FILAMENT_SERVICE_EXTRUDE_FEEDRATE_MM_MIN = 900
FILAMENT_SERVICE_RETRACT_FEEDRATE_MM_MIN = 2700
FILAMENT_SERVICE_SWAP_UNLOAD_FEEDRATE_MM_MIN = 240
FILAMENT_SERVICE_SWAP_LOAD_FEEDRATE_MM_MIN = 240
FILAMENT_SERVICE_HEAT_TIMEOUT_S = 240.0
FILAMENT_SERVICE_HEAT_POLL_S = 0.5
FILAMENT_SERVICE_HEAT_TOLERANCE_C = 5
FILAMENT_SERVICE_MANUAL_SWAP_MIN_TEMP_C = 130
FILAMENT_SERVICE_MANUAL_SWAP_MAX_TEMP_C = 150
Z_OFFSET_STEP_MM = 0.01
Z_OFFSET_REFRESH_TIMEOUT_S = 5.0
Z_OFFSET_CONFIRM_TIMEOUT_S = 8.0


def _filament_service_temp(profile):
    temp = (
        profile.get("nozzle_temp_other_layer")
        or profile.get("nozzle_temp_first_layer")
        or profile.get("nozzle_temp")
        or 0
    )
    try:
        temp = int(temp)
    except (TypeError, ValueError):
        temp = 0
    if temp <= 0:
        raise ValueError("Filament profile has no usable nozzle temperature")
    return temp


def _filament_service_length(payload, key):
    raw = payload.get(key, FILAMENT_SERVICE_DEFAULT_LENGTH_MM)
    try:
        length_mm = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if length_mm <= 0:
        raise ValueError(f"{key} must be greater than 0")
    if length_mm > FILAMENT_SERVICE_MAX_LENGTH_MM:
        raise ValueError(f"{key} must be <= {FILAMENT_SERVICE_MAX_LENGTH_MM:g}")
    return round(length_mm, 2)


def _filament_service_setting_length(settings, key, default=FILAMENT_SERVICE_DEFAULT_LENGTH_MM):
    try:
        return _filament_service_length({key: settings.get(key, default)}, key)
    except (AttributeError, ValueError):
        return round(default, 2)


def _normalize_filament_service_settings(settings):
    normalized = dict(settings or {})
    normalized["allow_legacy_swap"] = bool(normalized.get("allow_legacy_swap"))
    normalized["manual_swap_preheat_temp_c"] = _filament_service_manual_swap_temp(normalized)
    normalized["quick_move_length_mm"] = _filament_service_setting_length(normalized, "quick_move_length_mm")
    normalized["swap_unload_length_mm"] = _filament_service_setting_length(normalized, "swap_unload_length_mm")
    normalized["swap_load_length_mm"] = _filament_service_setting_length(normalized, "swap_load_length_mm")
    return normalized


def _filament_service_profile(payload, key):
    try:
        profile_id = int(payload.get(key))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer")
    profile = app.filaments.get(profile_id)
    if profile is None:
        raise LookupError(f"Filament profile {profile_id} not found")
    return profile


def _format_extrusion_mm(length_mm):
    text = f"{length_mm:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _build_filament_move_gcode(delta_mm, feedrate_mm_min=FILAMENT_SERVICE_FEEDRATE_MM_MIN):
    extrusion = _format_extrusion_mm(delta_mm)
    return "\n".join([
        "M83",
        f"G1 E{extrusion} F{int(feedrate_mm_min)}",
        "M400",
        "M82",
    ])


def _serialize_filament_swap_state(state):
    if not state:
        return {"pending": False, "swap": None}
    return {
        "pending": True,
        "swap": {
            "token": state["token"],
            "created_at": state["created_at"],
            "mode": state.get("mode", "legacy"),
            "phase": state.get("phase", "await_manual_swap"),
            "message": state.get("message"),
            "error": state.get("error"),
            "unload_profile_id": state["unload_profile_id"],
            "unload_profile_name": state["unload_profile_name"],
            "load_profile_id": state["load_profile_id"],
            "load_profile_name": state["load_profile_name"],
            "unload_temp_c": state["unload_temp_c"],
            "load_temp_c": state["load_temp_c"],
            "unload_length_mm": state["unload_length_mm"],
            "load_length_mm": state["load_length_mm"],
            "manual_swap_preheat_temp_c": state.get("manual_swap_preheat_temp_c"),
        },
    }


def _filament_swap_state_get(token=None):
    with app.filament_swap_lock:
        state = app.filament_swap_state
        if state is None:
            return None
        if token is not None and state.get("token") != token:
            return None
        return dict(state)


def _filament_swap_state_update(token, **updates):
    with app.filament_swap_lock:
        state = app.filament_swap_state
        if state is None or state.get("token") != token:
            return None
        state.update(updates)
        return dict(state)


def _filament_swap_state_clear(token=None):
    with app.filament_swap_lock:
        state = app.filament_swap_state
        if state is None:
            return None
        if token is not None and state.get("token") != token:
            return None
        app.filament_swap_state = None
        return dict(state)


def _filament_swap_start_background(target, token):
    thread = threading.Thread(target=target, args=(token,), daemon=True)
    thread.start()
    return thread


def _send_filament_service_gcode(gcode):
    with borrow_mqtt() as mqtt:
        if not mqtt:
            raise ConnectionError("MQTT service unavailable")
        if mqtt.is_printing:
            raise RuntimeError("Filament service commands are blocked while a print is active")
        mqtt.send_gcode(gcode)


def _assert_filament_service_ready(mqtt):
    if not mqtt:
        raise ConnectionError("MQTT service unavailable")
    if mqtt.is_printing:
        raise RuntimeError("Filament service commands are blocked while a print is active")


def _wait_for_filament_service_nozzle(mqtt, target_temp_c):
    deadline = time.monotonic() + FILAMENT_SERVICE_HEAT_TIMEOUT_S
    next_query = 0.0
    last_temp = mqtt.nozzle_temp
    target_ready = int(target_temp_c) - FILAMENT_SERVICE_HEAT_TOLERANCE_C

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_query:
            mqtt.request_status()
            next_query = now + 2.0

        current_temp = mqtt.nozzle_temp
        if current_temp is not None:
            last_temp = current_temp
            if current_temp >= target_ready:
                return current_temp

        time.sleep(FILAMENT_SERVICE_HEAT_POLL_S)

    raise TimeoutError(
        f"Nozzle did not reach {int(target_temp_c)}°C within {int(FILAMENT_SERVICE_HEAT_TIMEOUT_S)}s "
        f"(last seen: {last_temp if last_temp is not None else 'unknown'}°C)"
    )


def _filament_service_manual_swap_temp(settings):
    raw_temp = settings.get("manual_swap_preheat_temp_c", 140)
    try:
        temp_c = int(raw_temp)
    except (TypeError, ValueError):
        temp_c = 140
    return max(FILAMENT_SERVICE_MANUAL_SWAP_MIN_TEMP_C, min(FILAMENT_SERVICE_MANUAL_SWAP_MAX_TEMP_C, temp_c))


def _run_legacy_swap_unload(token):
    state = _filament_swap_state_get(token)
    if not state:
        return

    try:
        gcode = _build_filament_move_gcode(
            -state["unload_length_mm"],
            feedrate_mm_min=state.get("unload_feedrate_mm_min", FILAMENT_SERVICE_FEEDRATE_MM_MIN),
        )
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
            current_temp = mqtt.nozzle_temp
            if current_temp is None or current_temp < (state["unload_temp_c"] - FILAMENT_SERVICE_HEAT_TOLERANCE_C):
                _filament_swap_state_update(
                    token,
                    phase="heating_unload",
                    message=f"Heating nozzle to {state['unload_temp_c']}°C for unload...",
                    error=None,
                )
                mqtt.send_gcode(f"M104 S{state['unload_temp_c']}")
                _wait_for_filament_service_nozzle(mqtt, state["unload_temp_c"])

            _filament_swap_state_update(
                token,
                phase="unloading",
                message=(
                    f"Retracting {state['unload_length_mm']} mm for {state['unload_profile_name']}..."
                ),
                error=None,
            )
            mqtt.send_gcode(gcode)

        _filament_swap_state_update(
            token,
            phase="await_manual_swap",
            message=(
                "Unload finished. Release the extruder lever, remove the old filament, "
                "insert the new filament, then confirm."
            ),
            error=None,
        )
    except (RuntimeError, TimeoutError, ConnectionError) as exc:
        _filament_swap_state_update(
            token,
            phase="error",
            message=f"Automatic unload failed: {exc}",
            error=str(exc),
        )


def _run_legacy_swap_load(token):
    state = _filament_swap_state_get(token)
    if not state:
        return

    try:
        gcode = _build_filament_move_gcode(
            state["load_length_mm"],
            feedrate_mm_min=state.get("load_feedrate_mm_min", FILAMENT_SERVICE_FEEDRATE_MM_MIN),
        )
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
            current_temp = mqtt.nozzle_temp
            if current_temp is None or current_temp < (state["load_temp_c"] - FILAMENT_SERVICE_HEAT_TOLERANCE_C):
                _filament_swap_state_update(
                    token,
                    phase="heating_load",
                    message=f"Heating nozzle to {state['load_temp_c']}°C for load / purge...",
                    error=None,
                )
                mqtt.send_gcode(f"M104 S{state['load_temp_c']}")
                _wait_for_filament_service_nozzle(mqtt, state["load_temp_c"])

            _filament_swap_state_update(
                token,
                phase="loading",
                message=(
                    f"Loading / purging {state['load_profile_name']} "
                    f"({state['load_length_mm']} mm)..."
                ),
                error=None,
            )
            mqtt.send_gcode(gcode)

        _filament_swap_state_clear(token)
    except (RuntimeError, TimeoutError, ConnectionError) as exc:
        _filament_swap_state_update(
            token,
            phase="error",
            message=f"Automatic load / purge failed: {exc}",
            error=str(exc),
        )


def _z_offset_steps_to_mm(steps):
    if steps is None:
        return None
    return round(int(steps) * Z_OFFSET_STEP_MM, 2)


def _z_offset_mm_to_steps(mm_value):
    return int(round(mm_value / Z_OFFSET_STEP_MM))


def _format_signed_mm(mm_value):
    return f"{mm_value:+.2f}"


def _serialize_z_offset_state(state):
    state = dict(state or {})
    mm_value = state.get("mm")
    if mm_value is None:
        steps = state.get("steps")
        mm_value = _z_offset_steps_to_mm(steps)
        state["mm"] = mm_value
    state["display"] = f"{mm_value:.2f} mm" if mm_value is not None else "unknown"
    state.pop("seq", None)
    return state


def _parse_z_offset_mm(payload, key):
    if not isinstance(payload, dict) or key not in payload:
        raise ValueError(f"Missing {key}")
    try:
        value = float(payload[key])
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return round(value, 2)


def _set_printer_z_offset(mqtt, target_mm, current=None):
    target_steps = _z_offset_mm_to_steps(target_mm)
    if current is None:
        current = mqtt.refresh_z_offset(timeout=Z_OFFSET_REFRESH_TIMEOUT_S)
    current_steps = current["steps"]
    current_mm = current["mm"]
    delta_steps = target_steps - current_steps
    delta_mm = _z_offset_steps_to_mm(delta_steps)

    if delta_steps == 0:
        return {
            "status": "ok",
            "message": f"Z-offset already at {target_mm:.2f} mm.",
            "changed": False,
            "current": _serialize_z_offset_state(current),
            "target": {
                "steps": target_steps,
                "mm": _z_offset_steps_to_mm(target_steps),
                "display": f"{target_mm:.2f} mm",
            },
            "delta": {
                "steps": 0,
                "mm": 0.0,
                "display": "+0.00 mm",
            },
        }

    mqtt.send_gcode(f"M290 Z{_format_signed_mm(delta_mm)}")
    confirmed = mqtt.wait_for_z_offset_target(
        target_steps,
        after_seq=current["seq"],
        timeout=Z_OFFSET_CONFIRM_TIMEOUT_S,
    )
    mqtt.send_gcode("M500")

    return {
        "status": "ok",
        "message": (
            f"Z-offset moved from {current_mm:.2f} mm to {confirmed['mm']:.2f} mm "
            f"via M290 Z{_format_signed_mm(delta_mm)} and saved with M500."
        ),
        "changed": True,
        "current": _serialize_z_offset_state(current),
        "target": {
            "steps": target_steps,
            "mm": _z_offset_steps_to_mm(target_steps),
            "display": f"{target_mm:.2f} mm",
        },
        "delta": {
            "steps": delta_steps,
            "mm": delta_mm,
            "display": f"{delta_mm:+.2f} mm",
        },
        "confirmed": _serialize_z_offset_state(confirmed),
    }


# autopep8: off
import web.service.pppp
import web.service.video
import web.service.mqtt
import web.service.filetransfer
from web.service.filament import FilamentStore
# autopep8: on


def _validate_ws_auth(sock):
    """Check API key auth for WebSocket routes.

    Flask's before_request middleware does not run for WebSocket routes,
    so each handler must call this explicitly.  Auth succeeds if ANY of:
      - No API key is configured (backwards compatible)
      - Session cookie has authenticated=True
      - X-Api-Key header matches the configured key

    Returns True if authorized, False otherwise.  On failure, sends an
    error JSON message and the caller should return to close the socket.
    """
    api_key = app.config.get("api_key")
    if not api_key:
        return True
    if session.get("authenticated"):
        return True
    header_key = request.headers.get("X-Api-Key")
    if header_key and secrets.compare_digest(header_key, api_key):
        return True
    try:
        sock.send(json.dumps({"error": "unauthorized"}))
    except Exception as exc:
        log.debug(f"WS auth rejection send failed (client may have disconnected): {exc}")
    return False


@sock.route("/ws/mqtt")
def mqtt(sock):
    """
    Handles receiving and sending messages on the 'mqttqueue' stream service through websocket
    """
    if not app.config["login"] or app.config.get("unsupported_device"):
        return
    if not _validate_ws_auth(sock):
        return

    printer_index = _requested_printer_index()
    try:
        for data in stream_mqtt(printer_index):
            log.debug(f"MQTT message: {data}")
            sock.send(json.dumps(data))
    except ConnectionClosed:
        log.debug("/ws/mqtt closed by client")
    except OSError as exc:
        log.debug(f"/ws/mqtt closed during send: {exc}")
    except Exception as exc:
        log.warning(f"/ws/mqtt error: {exc}")
        log.info("Stack trace:", exc_info=True)


@sock.route("/ws/video")
def video(sock):
    """
    Handles receiving and sending messages on the 'videoqueue' stream service through websocket.

    Each connected client expresses intent to receive video by connecting here.
    video_enabled is set True on connect and cleared when the last client disconnects,
    so multiple tabs can independently enable/disable without interfering.
    """
    if not app.config["login"] or not app.config.get("video_supported") or app.config.get("unsupported_device"):
        return
    if not _validate_ws_auth(sock):
        return

    printer_index = _requested_printer_index()
    vq = get_video_service(printer_index)
    if not vq:
        return

    vq.viewer_connected()
    try:
        for msg in stream_videoqueue(printer_index, maxsize=VIDEO_STREAM_QUEUE_MAX):
            payload = getattr(msg, "data", None)
            if not payload:
                continue
            sock.send(payload)
    except ConnectionClosed:
        log.debug("/ws/video closed by client")
    except OSError as exc:
        log.debug(f"/ws/video closed during send: {exc}")
    except Exception as exc:
        log.warning(f"/ws/video error: {exc}")
        log.info("Stack trace:", exc_info=True)
    finally:
        # /ws/video is only a consumer of the shared video service.
        # The explicit owner of video enable/disable is /ws/ctrl via
        # {"video_enabled": true/false}.  Do not tear the service down here,
        # because transient websocket disconnects or reconnects would otherwise
        # stop live video for the whole app.
        try:
            vq.viewer_disconnected()
        except Exception as exc:
            log.debug(f"/ws/video cleanup failed: {exc}")


def _maybe_start_pppp_probe(reason="scheduled", printer_index=None):
    """Spawn a shared probe thread if one isn't already running and clients are watching."""
    import web.service.pppp as pppp_svc

    printer_index = _requested_printer_index() if printer_index is None else int(printer_index)
    probe = _get_pppp_probe_state(printer_index)
    with app.pppp_probe_lock:
        thread = probe["thread"]
        if thread is not None and thread.is_alive():
            return  # already running
        if probe["client_count"] <= 0:
            return  # no clients watching, don't probe

        config = app.config["config"]
        idx = printer_index

        def _run():
            result = pppp_svc.probe_pppp(config, idx)
            with app.pppp_probe_lock:
                probe["result"] = result
                probe["last_time"] = time.time()
                if result:
                    probe["fail_count"] = 0
                else:
                    probe["fail_count"] += 1
                fail_count = probe["fail_count"]
            log.info(f"PPPP probe result: {'ok' if result else 'fail'} (fail_count={fail_count})")

        t = threading.Thread(target=_run, daemon=True)
        probe["thread"] = t
        t.start()
        log.info(f"Starting PPPP probe ({reason}, fail_count={probe['fail_count']})")


@sock.route("/ws/pppp-state")
def pppp_state(sock):
    """
    Provides the status of the 'pppp' stream service through websocket.

    Uses a passive read of the service registry so this handler never holds
    a PPPP ref and never starts the service on its own.  That way the phone
    app can open its own PPPP session even while the web UI is open.

    When MQTT has been silent for >30 seconds and PPPP is not actively
    connected, a background probe is triggered (at most once per 60s) to
    check if the printer is reachable on the LAN.

    States emitted:
      "dormant"      — service not running (video/timelapse not active)
      "connected"    — service running and PPPP handshake complete, or probe succeeded
      "disconnected" — service was connected but the connection was lost, or probe failed
    """
    if not app.config["login"] or app.config.get("unsupported_device"):
        log.info("Websocket connection rejected: no printer configured (use 'config import' or 'config login')")
        return
    if not _validate_ws_auth(sock):
        return

    printer_index = _requested_printer_index()
    log.info("Starting PPPP state websocket handler")

    last_status = None
    last_keepalive = 0.0
    pppp_was_connected = False  # True once we see "connected"; resets on dormant
    mqtt_was_stale = False      # tracks previous stale state to detect recovery

    # Scheduling constants
    PROBE_INTERVAL = 60.0    # back-off interval after MAX_RETRIES failures
    RETRY_INTERVAL = 15.0    # interval between retries after a failure
    MQTT_STALE_AFTER = 30.0  # MQTT considered stale after 30s silence
    MAX_RETRIES = 2          # retries after first failure before switching to PROBE_INTERVAL

    # Register this client and kick off an immediate probe if we're the first.
    probe = _get_pppp_probe_state(printer_index)
    with app.pppp_probe_lock:
        probe["client_count"] += 1
        is_first = probe["client_count"] == 1

    if is_first:
        _maybe_start_pppp_probe("first client", printer_index=printer_index)

    try:
        while True:
            now = time.time()

            # Passive read — no ref-count increment, never starts the service.
            probe = _get_pppp_probe_state(printer_index)
            pppp = get_pppp_service(printer_index)

            if pppp is not None and bool(getattr(pppp, "connected", False)):
                current_status = "connected"
                pppp_was_connected = True
                with app.pppp_probe_lock:
                    probe["result"] = None
                    probe["fail_count"] = 0
            else:
                # Check MQTT staleness and detect recovery transition
                mqtt_svc = get_mqtt_service(printer_index)
                mqtt_last = getattr(mqtt_svc, "last_message_time", 0.0) if mqtt_svc else 0.0
                mqtt_stale = mqtt_last > 0 and (now - mqtt_last) > MQTT_STALE_AFTER

                mqtt_recovered = mqtt_was_stale and not mqtt_stale
                if mqtt_recovered:
                    log.info("MQTT recovered — resetting PPPP probe state")
                    with app.pppp_probe_lock:
                        probe["result"] = None
                        probe["fail_count"] = 0
                mqtt_was_stale = mqtt_stale

                # Snapshot shared probe state under lock
                with app.pppp_probe_lock:
                    probe_result = probe["result"]
                    last_probe_time = probe["last_time"]
                    probe_fail_count = probe["fail_count"]

                # Short retries for first MAX_RETRIES failures; long back-off once the printer is clearly offline
                next_interval = RETRY_INTERVAL if probe_fail_count <= MAX_RETRIES else PROBE_INTERVAL

                # Also probe when PPPP was recently connected but service stopped
                # (e.g. last video client disconnected) so the badge refreshes.
                pppp_went_dormant = pppp_was_connected and probe_result is None

                should_probe = (
                    (mqtt_stale or mqtt_recovered or probe_result is False or pppp_went_dormant)
                    and (now - last_probe_time) > next_interval
                )
                if should_probe:
                    reason = ("PPPP service stopped" if pppp_went_dormant
                              else "MQTT recovered" if mqtt_recovered
                              else "MQTT stale" if mqtt_stale
                              else "retry after fail")
                    _maybe_start_pppp_probe(reason, printer_index=printer_index)

                if probe_result is True:
                    current_status = "connected"
                elif probe_result is False:
                    current_status = "disconnected"
                elif pppp is not None and getattr(pppp, "wanted", False) and pppp_was_connected:
                    # Service is still wanted but lost its PPPP connection.
                    current_status = "disconnected"
                else:
                    # Service not running or connecting for the first time → dormant.
                    current_status = "dormant"
                    if pppp is None or not getattr(pppp, "wanted", False):
                        pppp_was_connected = False

            if current_status != last_status or (current_status == "connected" and now - last_keepalive >= 10.0):
                sock.send(json.dumps({"status": current_status}))
                last_status = current_status
                if current_status == "connected":
                    last_keepalive = now

            time.sleep(1.0)
    except ConnectionClosed:
        log.debug("WebSocket connection closed by client")
    except Exception as e:
        log.warning(f"Error in PPPP state websocket handler: {e}")
        log.info("Stack trace:", exc_info=True)
    finally:
        try:
            probe = _get_pppp_probe_state(printer_index)
            with app.pppp_probe_lock:
                probe["client_count"] = max(0, int(probe["client_count"]) - 1)
        except Exception as exc:
            log.debug(f"PPPP state cleanup failed: {exc}")
        log.debug("PPPP state websocket handler ending")


@sock.route("/ws/upload")
def upload(sock):
    """
    Provides upload progress updates through websocket
    """
    if not app.config["login"] or app.config.get("unsupported_device"):
        return
    if not _validate_ws_auth(sock):
        return

    try:
        for data in app.svc.stream("filetransfer"):
            sock.send(json.dumps(data))
    except ConnectionClosed:
        log.debug("/ws/upload closed by client")
    except OSError as exc:
        log.debug(f"/ws/upload closed during send: {exc}")
    except Exception as exc:
        log.warning(f"/ws/upload error: {exc}")
        log.info("Stack trace:", exc_info=True)


@sock.route("/ws/ctrl")
def ctrl(sock):
    """
    Handles controlling of light and video quality through websocket
    """
    if not app.config["login"] or app.config.get("unsupported_device"):
        return
    if not _validate_ws_auth(sock):
        return

    # send a response on connect, to let the client know the connection is ready
    sock.send(json.dumps({"ankerctl": 1}))
    printer_index = _requested_printer_index()
    vq = get_video_service(printer_index)
    if vq:
        profile_id = getattr(vq, "saved_video_profile_id", None)
        if profile_id is None:
            profile_id = web.service.video.VIDEO_PROFILE_DEFAULT_ID
        sock.send(json.dumps({"video_profile": profile_id}))

    while True:
        try:
            data = sock.receive()
            if data is None:
                break
            msg = json.loads(data)
        except ConnectionClosed:
            break
        except (json.JSONDecodeError, TypeError) as exc:
            log.warning(f"/ws/ctrl: malformed message, ignoring: {exc}")
            continue

        if "light" in msg:
            if isinstance(msg["light"], bool):
                with borrow_videoqueue(printer_index) as vq:
                    vq.api_light_state(msg["light"])
            else:
                log.warning(f"Invalid 'light' value (expected bool): {msg['light']!r}")

        if "video_profile" in msg:
            if isinstance(msg["video_profile"], str):
                with borrow_videoqueue(printer_index) as vq:
                    vq.api_video_profile(msg["video_profile"])
            else:
                log.warning(f"Invalid 'video_profile' value (expected str): {msg['video_profile']!r}")
        elif "quality" in msg:
            if isinstance(msg["quality"], int):
                with borrow_videoqueue(printer_index) as vq:
                    vq.api_video_mode(msg["quality"])
            else:
                log.warning(f"Invalid 'quality' value (expected int): {msg['quality']!r}")

        if "video_enabled" in msg:
            if not isinstance(msg["video_enabled"], bool):
                log.warning(f"Invalid 'video_enabled' value (expected bool): {msg['video_enabled']!r}")
                continue
            vq = get_video_service(printer_index)
            if vq:
                vq.set_video_enabled(msg["video_enabled"])


@app.get("/video")
def video_download():
    """
    Handles the video streaming/downloading feature in the Flask app
    """
    # Enforce API key auth when configured; timelapse client uses ?for_timelapse=1
    # on the loopback interface and does not carry a session, so allow it only when
    # the request comes from localhost.
    api_key = app.config.get("api_key")
    if api_key:
        _hdr = request.headers.get("X-Api-Key", "")
        _qry = request.args.get("apikey", "")
        authed = (
            session.get("authenticated")
            or (_hdr and secrets.compare_digest(_hdr, api_key))
            or (_qry and secrets.compare_digest(_qry, api_key))
        )
        if not authed:
            log.warning("/video rejected: missing or invalid API key")
            return Response("Unauthorized", 401)

    for_timelapse = request.args.get("for_timelapse") == "1"
    printer_index = request.args.get("printer_index", default=app.config.get("printer_index", 0), type=int)

    def generate():
        if not app.config["login"] or not _printer_video_supported(printer_index=printer_index):
            return
        vq = get_video_service(printer_index)
        if vq:
            if not for_timelapse and not getattr(vq, "video_enabled", False):
                return
            if vq.state == RunState.Stopped:
                try:
                    vq.start()
                    vq.await_ready()
                except ServiceStoppedError:
                    return
        for msg in stream_videoqueue(printer_index, maxsize=VIDEO_STREAM_QUEUE_MAX):
            yield msg.data

    return Response(generate(), mimetype="video/mp4")


@app.get("/")
def app_root():
    """
    Renders the html template for the root route, which is the homepage of the Flask app
    """
    config = app.config["config"]
    with config.open() as cfg:
        user_agent = user_agent_parse(request.headers.get("User-Agent"))
        user_os = web.platform.os_platform(user_agent.os.family)

        printers_list = []
        if cfg:
            anker_config = str(web.config.config_show(cfg))
            config_existing_email = cfg.account.email if cfg.account else ""
            printer = cfg.printers[app.config["printer_index"]]
            upload_rate_mbps, upload_rate_source = cli.util.resolve_upload_rate_mbps_with_source(cfg)
            upload_rate_config = getattr(cfg, "upload_rate_mbps", None)
            country = cfg.account.country if cfg.account else ""
            for i, p in enumerate(cfg.printers):
                printers_list.append({
                    "index": i,
                    "name": p.name,
                    "sn": p.sn,
                    "model": p.model,
                    "supported": p.model not in UNSUPPORTED_PRINTERS,
                })
        else:
            anker_config = "No printers found, please load your login config..."
            config_existing_email = ""
            printer = None
            upload_rate_mbps = None
            upload_rate_config = None
            upload_rate_source = None
            country = ""

        active_camera = _resolve_camera_settings(cfg, printer_index=app.config.get("printer_index", 0)) if cfg else _resolve_camera_settings(None)
        printer_video_supported = bool(app.config.get("video_supported", False))
        show_camera_ffmpeg_warning = bool(
            printer_video_supported
            or (active_camera.get("external") or {}).get("configured")
        )

        if ":" in request.host:
            request_host, request_port = request.host.split(":", 1)
        else:
            request_host = request.host
            request_port = "80"

        return render_template(
            "index.html",
            request_host=request_host,
            request_port=request_port,
            configure=app.config["login"],
            login_file_path=web.platform.login_path(user_os),
            anker_config=anker_config,
            config_existing_email=config_existing_email,
            country_codes=json.dumps(cli.countrycodes.country_codes),
            current_country=country,
            video_supported=app.config.get("video_supported", False),
            printer_video_supported=printer_video_supported,
            camera_features_available=bool(active_camera.get("feature_available")),
            active_camera=active_camera,
            show_camera_ffmpeg_warning=show_camera_ffmpeg_warning,
            ffmpeg_available=_ffmpeg_available(),
            upload_rate_mbps=upload_rate_mbps,
            upload_rate_config=upload_rate_config,
            upload_rate_source=upload_rate_source,
            upload_rate_env=os.getenv("UPLOAD_RATE_MBPS"),
            upload_rate_choices=UPLOAD_RATE_MBPS_CHOICES,
            printer=printer,
            video_profiles=web.service.video.VIDEO_PROFILES,
            video_profile_default=web.service.video.VIDEO_PROFILE_DEFAULT_ID,
            printers=printers_list,
            active_printer_index=app.config["printer_index"],
            printer_index_locked=app.config.get("printer_index_locked", False),
            unsupported_device=app.config.get("unsupported_device", False),
            ankerctl_root=os.path.realpath(ROOT_DIR),
        )


@app.get("/api/health")
def app_api_health():
    """Lightweight liveness probe — always returns 200 OK (no auth required)."""
    return {"status": "ok"}


@app.get("/api/printers")
def app_api_printers():
    """Return list of configured printers and the currently active index."""
    config = app.config["config"]
    with config.open() as cfg:
        printers = []
        if cfg:
            for i, p in enumerate(cfg.printers):
                printers.append({
                    "index": i,
                    "name": p.name,
                    "sn": p.sn,
                    "model": p.model,
                    "ip_addr": p.ip_addr,
                    "supported": p.model not in UNSUPPORTED_PRINTERS,
                })
        return jsonify({
            "printers": printers,
            "active_index": app.config["printer_index"],
            "locked": app.config.get("printer_index_locked", False),
        })


@app.post("/api/printers/lan-search")
def app_api_printers_lan_search():
    """Broadcast LAN search, persist matching printer IPs, and report findings."""
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg or not getattr(cfg, "printers", None):
            return jsonify({"error": "No printers configured"}), 400
        active_index = app.config.get("printer_index", 0)
        active_printer = cfg.printers[active_index] if active_index < len(cfg.printers) else None

    discovered = cli.pppp.lan_search(config, timeout=1.0, dumpfile=app.config.get("pppp_dump"))
    if not discovered:
        return jsonify({
            "error": "No printers responded within timeout. Are you connected to the same network as the printer?",
        }), 404

    active_result = None
    if active_printer:
        for result in discovered:
            if result["duid"] == active_printer.p2p_duid:
                active_result = result
                break

    return jsonify({
        "status": "ok",
        "discovered": discovered,
        "saved_count": sum(1 for item in discovered if item["persisted"]),
        "active_printer": {
            "name": getattr(active_printer, "name", None),
            "duid": getattr(active_printer, "p2p_duid", None),
            "ip_addr": active_result["ip_addr"] if active_result else getattr(active_printer, "ip_addr", ""),
            "updated": bool(active_result),
        },
    })


@app.post("/api/printers/active")
def app_api_set_active_printer():
    """Switch the active printer. Blocked when PRINTER_INDEX env var is set."""
    if app.config.get("printer_index_locked"):
        return jsonify({"error": "Printer selection locked by PRINTER_INDEX environment variable"}), 403

    payload = request.get_json(silent=True) or {}
    new_index = payload.get("index")
    if not isinstance(new_index, int):
        return jsonify({"error": "Missing or invalid 'index' parameter"}), 400

    config = app.config["config"]
    with config.open() as cfg:
        if not cfg or new_index < 0 or new_index >= len(cfg.printers):
            return jsonify({"error": f"Printer index {new_index} out of range"}), 400

        # Block switching to an unsupported device (e.g. eufyMake E1 UV printer)
        if cfg.printers[new_index].model in UNSUPPORTED_PRINTERS:
            return jsonify({
                "error": f"Device {cfg.printers[new_index].model} is not supported by ankerctl"
            }), 403

        printer = cfg.printers[new_index]
        video_supported = printer.model not in PRINTERS_WITHOUT_CAMERA
        unsupported = printer.model in UNSUPPORTED_PRINTERS

    old_index = app.config["printer_index"]
    if new_index == old_index:
        return jsonify({"status": "ok", "message": "Already active"})

    # Update in-memory state
    app.config["printer_index"] = new_index
    app.config["video_supported"] = video_supported
    app.config["unsupported_device"] = unsupported

    # Persist selection to config file
    with config.modify() as cfg:
        cfg.active_printer_index = new_index

    rich_service_manager = (
        hasattr(app.svc, "svcs")
        and hasattr(app.svc, "register")
        and hasattr(app.svc, "unregister")
    )
    if rich_service_manager:
        # Per-printer video/PPPP services stay attached to their own printer so
        # background timelapses can continue even when the UI switches printers.
        register_services(app)
    else:
        restart_all = getattr(app.svc, "restart_all", None)
        if restart_all is not None:
            restart_all(await_ready=False)

    log.info(f"Switched active printer: index {old_index} -> {new_index} ({printer.name})")
    return jsonify({"status": "ok", "printer": {"index": new_index, "name": printer.name, "sn": printer.sn}})


@app.get("/api/version")
def app_api_version():
    """
    Returns the version details of api and server as dictionary

    Returns:
        A dictionary containing version details of api and server
    """
    return {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0"}


@app.post("/api/ankerctl/config/upload")
def app_api_ankerctl_config_upload():
    """
    Handles the uploading of configuration file to Flask server

    Returns:
        A HTML redirect response
    """
    if "login_file" not in request.files:
        return web.util.flash_redirect(url_for('app_root'), "No file found", "danger")
    file = request.files["login_file"]

    try:
        web.config.config_import(file, app.config["config"])
        session["authenticated"] = True
        return web.util.flash_redirect(url_for('app_api_ankerctl_server_reload'),
                                       "Configuration imported!", "success")
    except web.config.ConfigImportError as err:
        log.exception(f"Config import failed: {err}")
        return web.util.flash_redirect(url_for('app_root'), "Config import failed. Check server logs for details.", "danger")
    except Exception as err:
        log.exception(f"Config import failed: {err}")
        return web.util.flash_redirect(url_for('app_root'), "An unexpected error occurred. Check server logs for details.", "danger")


@app.post("/api/ankerctl/config/login")
def app_api_ankerctl_config_login():
    form_data = request.form.to_dict()

    for key in ["login_email", "login_password", "login_country"]:
        if key not in form_data:
            return jsonify({"error": f"Error: Missing form entry '{key}'"})

    if not cli.countrycodes.code_to_country(form_data["login_country"]):
        return jsonify({"error": f"Error: Invalid country code '{form_data['login_country']}'"})

    try:
        web.config.config_login(
            form_data['login_email'],
            form_data['login_password'],
            form_data['login_country'],
            form_data.get('login_captcha_id', ''),
            form_data.get('login_captcha_text', ''),
            app.config["config"],
        )
        flash("Configuration imported!", "success")
        session["authenticated"] = True
        return jsonify({"redirect": url_for('app_api_ankerctl_server_reload')})
    except web.config.ConfigImportError as err:
        if err.captcha:
            return jsonify({"captcha_id": err.captcha["id"], "captcha_url": err.captcha["img"]})
        log.exception(f"Config login failed: {err}")
        flash("Login failed. Check server logs for details.", "danger")
        return jsonify({"redirect": url_for('app_root')})
    except Exception as err:
        log.exception(f"Config login failed: {err}")
        flash("An unexpected error occurred. Check server logs for details.", "danger")
        return jsonify({"redirect": url_for('app_root')})


@app.get("/api/ankerctl/server/reload")
def app_api_ankerctl_server_reload():
    """
    Reloads the Flask server

    Returns:
        A HTML redirect response
    """
    config = app.config["config"]

    with config.open() as cfg:
        app.config["login"] = bool(cfg)
        if not cfg:
            return web.util.flash_redirect(url_for('app_root'), "No printers found in config", "warning")
        if "_flashes" in session:
            session["_flashes"].clear()

        printer_index = app.config.get("printer_index", 0)
        active_model = cfg.printers[printer_index].model if printer_index < len(cfg.printers) else None
        app.config["video_supported"] = bool(active_model and active_model not in PRINTERS_WITHOUT_CAMERA)
        unsupported = active_model in UNSUPPORTED_PRINTERS
        app.config["unsupported_device"] = unsupported
        if unsupported:
            log.warning(
                f"Active device {active_model} is not supported by ankerctl — "
                "only supported printers will get background MQTT observers."
            )

        if not app.svc.svcs:
            register_services(app)

        try:
            app.svc.restart_all(await_ready=False)
        except Exception as err:
            log.exception(err)
            return web.util.flash_redirect(url_for('app_root'), f"Ankerctl could not be reloaded: {err}", "danger")

        return web.util.flash_redirect(url_for('app_root'), "Ankerctl reloaded successfully", "success")


@app.post("/api/files/local")
def app_api_files_local():
    """
    Handles the uploading of files to Flask server

    Returns:
        A dictionary containing file details
    """
    user_name = request.headers.get("User-Agent", "ankerctl").split(url_for('app_root'))[0]

    try:
        no_act = not cli.util.parse_http_bool(request.form.get("print", "false"))
    except ValueError:
        return {"error": "Invalid value for 'print' field"}, 400

    fd = request.files["file"]
    # Snapshot the active printer index at request time so the upload targets the
    # correct printer even if the user switches printers mid-transfer.
    printer_index = app.config.get("printer_index", 0)
    with app.config["config"].open() as cfg:
        rate_limit_mbps, rate_limit_source = cli.util.resolve_upload_rate_mbps_with_source(cfg)

    with app.svc.borrow("filetransfer") as ft:
        try:
            ft.send_file(fd, user_name, rate_limit_mbps=rate_limit_mbps, start_print=not no_act, printer_index=printer_index)
        except ConnectionError as E:
            log.error(f"Connection error: {E}")
            # This message will be shown in i.e. PrusaSlicer, so attempt to
            # provide a readable explanation.
            cli.util.http_abort(
                503,
                "Cannot connect to printer!\n" \
                "\n" \
                "Please verify that printer is online, and on the same network as ankerctl."
            )

    return {
        "status": "ok",
        "upload_rate_mbps": rate_limit_mbps,
        "upload_rate_source": rate_limit_source,
    }


@app.get("/api/files/printer")
def app_api_files_printer():
    source = str(request.args.get("source", "onboard") or "onboard").strip().lower()
    if source not in {"onboard", "usb"}:
        return {"error": "Invalid storage source"}, 400
    raw_value = request.args.get("value")
    source_value = None
    if raw_value not in (None, ""):
        try:
            source_value = int(raw_value)
        except ValueError:
            return {"error": "value must be an integer"}, 400

    result, error = _probe_printer_storage_files(source=source, source_value=source_value)
    if error:
        payload, status = error
        return payload, status

    resolved_source = "onboard" if result["source_value"] == 1 else source
    files = []
    for entry in result["files"]:
        item = dict(entry)
        item["thumbnail_url"] = url_for(
            "app_api_files_printer_thumbnail",
            path=item.get("path", ""),
            source=resolved_source,
        )
        files.append(item)

    return {
        "status": "ok",
        "source": resolved_source,
        "source_value": result["source_value"],
        "reply_count": result["reply_count"],
        "files": files,
    }


@app.get("/api/files/printer/thumbnail")
def app_api_files_printer_thumbnail():
    source = str(request.args.get("source", "") or "").strip().lower() or None
    try:
        file_path, _ = _validate_printer_storage_path(request.args.get("path"), source=source)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        preview_url = mqtt.get_cached_stored_file_preview_url(file_path)
        if not preview_url:
            allow_probe = not (mqtt.is_printing or mqtt.has_pending_print_start or mqtt.is_preparing_print)
            preview_url = mqtt.get_stored_file_preview_url(file_path, allow_probe=allow_probe)

    if not preview_url:
        return {"error": "Thumbnail not available for this stored file"}, 404

    return _proxy_preview_image_response(preview_url)


@app.post("/api/files/printer/print")
def app_api_files_printer_print():
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "") or "").strip().lower() or None
    if source is not None and source not in {"onboard", "usb"}:
        return {"error": "Invalid storage source"}, 400

    try:
        file_path, inferred_source = _validate_printer_storage_path(payload.get("path"), source=source)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    with borrow_mqtt() as mqtt:
        if mqtt.is_printing or mqtt.has_pending_print_start or mqtt.is_preparing_print:
            return {"error": "Printer is already busy with another print job"}, 409
        started = mqtt.start_stored_file(file_path)

    if not started:
        return {
            "error": (
                "Selected file preview loaded, but the printer did not confirm the job start. "
                "Stored-file launching is still incomplete for this firmware."
            )
        }, 504

    return {
        "status": "ok",
        "source": inferred_source,
        "path": file_path,
        "name": os.path.basename(file_path),
    }


@app.post("/api/ankerctl/config/upload-rate")
def app_api_ankerctl_config_upload_rate():
    config = app.config["config"]
    if "upload_rate_mbps" not in request.form:
        return {"error": "upload_rate_mbps missing"}, 400

    try:
        rate_limit_mbps = int(request.form["upload_rate_mbps"])
    except ValueError:
        return {"error": "upload_rate_mbps must be an integer"}, 400

    if rate_limit_mbps not in UPLOAD_RATE_MBPS_CHOICES:
        return {"error": f"upload_rate_mbps must be one of {', '.join(map(str, UPLOAD_RATE_MBPS_CHOICES))}"}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        cfg.upload_rate_mbps = rate_limit_mbps

    with config.open() as cfg:
        effective_rate_mbps, effective_rate_source = cli.util.resolve_upload_rate_mbps_with_source(cfg)

    return {
        "status": "ok",
        "upload_rate_mbps": rate_limit_mbps,
        "effective_upload_rate_mbps": effective_rate_mbps,
        "effective_upload_rate_source": effective_rate_source,
    }


@app.get("/api/notifications/settings")
def app_api_notifications_settings():
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        apprise_config = _resolve_apprise(cfg)

    return {"apprise": apprise_config}


@app.post("/api/notifications/settings")
def app_api_notifications_settings_update():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    apprise_payload = payload.get("apprise") if "apprise" in payload else payload
    if not isinstance(apprise_payload, dict):
        return {"error": "Invalid apprise payload"}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        notifications = _resolve_notifications(cfg)
        apprise_config = _resolve_apprise(cfg)
        apprise_config = _deep_update(apprise_config, apprise_payload)
        notifications["apprise"] = apprise_config
        cfg.notifications = notifications

    return {"status": "ok", "apprise": apprise_config}


@app.post("/api/notifications/test")
def app_api_notifications_test():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    apprise_payload = None
    if isinstance(payload, dict):
        apprise_payload = payload.get("apprise") if "apprise" in payload else payload
        if apprise_payload is not None and not isinstance(apprise_payload, dict):
            return {"error": "Invalid apprise payload"}, 400

    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        apprise_config = _resolve_apprise(cfg)

    if apprise_payload is not None:
        apprise_config = _deep_update(apprise_config, apprise_payload)

    # Use explicit settings for snapshot generation test
    from web.notifications import AppriseNotifier
    notifier = AppriseNotifier(config, settings=apprise_config)
    attachments, cleanup = notifier.build_attachments()

    client = AppriseClient(apprise_config)
    # Manually send via _post to bypass event checks for testing
    ok, message = client._post("Ankerctl Test", "Test notification sent from ankerctl settings page.", attachments=attachments)

    notifier.cleanup_attachments(cleanup)
    if ok:
        return {"status": "ok", "message": message}
    return {"error": message}, 400


@app.get("/api/settings/camera")
def app_api_settings_camera():
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        camera_config = _resolve_camera_settings(cfg)
    return {"camera": camera_config}


@app.post("/api/settings/camera")
def app_api_settings_camera_update():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    camera_payload = payload.get("camera") if "camera" in payload else payload
    if not isinstance(camera_payload, dict):
        return {"error": "Invalid camera payload"}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        try:
            camera_config = web.camera.update_camera_settings(cfg, app.config.get("printer_index", 0), camera_payload)
        except ValueError as exc:
            return {"error": str(exc)}, 400

    return {"status": "ok", "camera": camera_config}


@app.post("/api/settings/launcher-bat")
def app_api_settings_launcher_bat():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    install_dir = payload.get("install_dir")
    try:
        script = _build_windows_launcher_bat(install_dir)
    except ValueError as exc:
        return {"error": str(exc)}, 400

    return Response(
        script,
        mimetype="text/plain",
        headers={
            "Content-Disposition": 'attachment; filename="ankerctl-launcher.bat"',
        },
    )


@app.get("/api/settings/timelapse")
def app_api_settings_timelapse():
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        timelapse_config = cli.model.merge_dict_defaults(
            getattr(cfg, "timelapse", {}),
            cli.model.default_timelapse_config()
        )
    return {"timelapse": timelapse_config}


@app.post("/api/settings/timelapse")
def app_api_settings_timelapse_update():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    tl_payload = payload.get("timelapse") if "timelapse" in payload else payload
    if not isinstance(tl_payload, dict):
        return {"error": "Invalid timelapse payload"}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        
        current = cli.model.merge_dict_defaults(
            getattr(cfg, "timelapse", {}),
            cli.model.default_timelapse_config()
        )
        # Deep update not strictly needed if structure is flat, but good practice
        new_config = _deep_update(current, tl_payload)
        cfg.timelapse = new_config

    # Reload all printer-local timelapse helpers.
    for _, mqtt in iter_mqtt_services():
        if mqtt and mqtt.timelapse:
            mqtt.timelapse.reload_config(config)

    return {"status": "ok", "timelapse": new_config}


@app.get("/api/settings/mqtt")
def app_api_settings_mqtt():
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        ha_config = cli.model.merge_dict_defaults(
            getattr(cfg, "home_assistant", {}),
            cli.model.default_home_assistant_config()
        )
    return {"home_assistant": ha_config}


@app.post("/api/settings/mqtt")
def app_api_settings_mqtt_update():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    ha_payload = payload.get("home_assistant") if "home_assistant" in payload else payload
    if not isinstance(ha_payload, dict):
        return {"error": "Invalid home_assistant payload"}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        
        current = cli.model.merge_dict_defaults(
            getattr(cfg, "home_assistant", {}),
            cli.model.default_home_assistant_config()
        )
        new_config = _deep_update(current, ha_payload)
        cfg.home_assistant = new_config

    # Reload all printer-local Home Assistant bridges.
    for _, mqtt in iter_mqtt_services():
        if mqtt and mqtt.ha:
            mqtt.ha.reload_config(config)

    return {"status": "ok", "home_assistant": new_config}


@app.get("/api/settings/filament-service")
def app_api_settings_filament_service():
    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        filament_config = _normalize_filament_service_settings(_resolve_filament_service_settings(cfg))
    return {"filament_service": filament_config}


@app.post("/api/settings/filament-service")
def app_api_settings_filament_service_update():
    config = app.config["config"]
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return {"error": "Invalid JSON payload"}, 400

    fs_payload = payload.get("filament_service") if "filament_service" in payload else payload
    if not isinstance(fs_payload, dict):
        return {"error": "Invalid filament_service payload"}, 400

    if "allow_legacy_swap" in fs_payload:
        fs_payload["allow_legacy_swap"] = bool(fs_payload["allow_legacy_swap"])
    if "manual_swap_preheat_temp_c" in fs_payload:
        try:
            fs_payload["manual_swap_preheat_temp_c"] = int(fs_payload["manual_swap_preheat_temp_c"])
        except (TypeError, ValueError):
            return {"error": "manual_swap_preheat_temp_c must be an integer"}, 400
    for key in ("quick_move_length_mm", "swap_unload_length_mm", "swap_load_length_mm"):
        if key in fs_payload:
            try:
                fs_payload[key] = _filament_service_length({key: fs_payload[key]}, key)
            except ValueError as exc:
                return {"error": str(exc)}, 400

    with config.modify() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400

        current = _resolve_filament_service_settings(cfg)
        new_config = _deep_update(current, fs_payload)
        new_config = _normalize_filament_service_settings(new_config)
        cfg.filament_service = new_config

    return {"status": "ok", "filament_service": new_config}


# GCode prefixes that are unsafe to send while a print is active
_UNSAFE_GCODE_PREFIXES = {"G0", "G1", "G28", "G29", "G91", "G90"}

@app.post("/api/printer/gcode")
def app_api_printer_gcode():
    payload = request.get_json(silent=True)
    if not payload or "gcode" not in payload:
        return {"error": "Missing gcode"}, 400

    gcode = payload["gcode"]
    if not isinstance(gcode, str):
        return {"error": "gcode must be a string"}, 400

    lines = cli.util.normalize_gcode_lines(gcode)
    if not lines:
        return {"error": "No executable gcode lines found"}, 400

    normalized_gcode = "\n".join(lines)

    with borrow_mqtt() as mqtt:
        if mqtt.is_printing:
            unsafe = [l for l in lines if l.split()[0].upper() in _UNSAFE_GCODE_PREFIXES]
            if unsafe:
                return {"error": "Motion commands blocked while printing"}, 409
        mqtt.send_gcode(normalized_gcode)

    return {"status": "ok"}


@app.post("/api/printer/home")
def app_api_printer_home():
    payload = request.get_json(silent=True) or {}
    axis = str(payload.get("axis", "all")).lower()
    if axis not in {"all", "xy", "z"}:
        return {"error": "Invalid home axis"}, 400

    with borrow_mqtt() as mqtt:
        if mqtt.is_printing:
            return {"error": "Motion commands blocked while printing"}, 409
        mqtt.send_home(axis)

    return {"status": "ok", "axis": axis}


@app.post("/api/printer/control")
def app_api_printer_control():
    payload = request.get_json(silent=True)
    if not payload or "value" not in payload:
        return {"error": "Missing value"}, 400

    try:
        value = int(payload["value"])
    except (ValueError, TypeError):
        return {"error": "Value must be an integer"}, 400
    if value not in {0, 2, 3, 4}:
        return {"error": "Invalid control value"}, 400

    with borrow_mqtt() as mqtt:
        mqtt.send_print_control(value)

    return {"status": "ok"}


@app.post("/api/printer/autolevel")
def app_api_printer_autolevel():
    with borrow_mqtt() as mqtt:
        if mqtt.is_printing:
            return {"error": "Auto-leveling blocked while printing"}, 409
        mqtt.send_auto_leveling()
    return {"status": "ok"}


@app.get("/api/printer/z-offset")
def app_api_printer_z_offset():
    with borrow_mqtt() as mqtt:
        state = mqtt.get_z_offset_state()
        if not state.get("available"):
            try:
                state = mqtt.refresh_z_offset(timeout=Z_OFFSET_REFRESH_TIMEOUT_S)
            except TimeoutError:
                state = mqtt.get_z_offset_state()
        return {
            "status": "ok",
            "z_offset": _serialize_z_offset_state(state),
        }


@app.post("/api/printer/z-offset/refresh")
def app_api_printer_z_offset_refresh():
    with borrow_mqtt() as mqtt:
        try:
            state = mqtt.refresh_z_offset(timeout=Z_OFFSET_REFRESH_TIMEOUT_S)
        except TimeoutError as exc:
            return {"error": str(exc)}, 504
        return {
            "status": "ok",
            "message": f"Read live Z-offset {state['mm']:.2f} mm from MQTT 1021.",
            "z_offset": _serialize_z_offset_state(state),
        }


@app.post("/api/printer/z-offset")
def app_api_printer_z_offset_set():
    payload = request.get_json(silent=True)
    try:
        target_mm = _parse_z_offset_mm(payload, "target_mm")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    with borrow_mqtt() as mqtt:
        try:
            return _set_printer_z_offset(mqtt, target_mm)
        except TimeoutError as exc:
            return {"error": str(exc)}, 504


@app.post("/api/printer/z-offset/nudge")
def app_api_printer_z_offset_nudge():
    payload = request.get_json(silent=True)
    try:
        delta_mm = _parse_z_offset_mm(payload, "delta_mm")
    except ValueError as exc:
        return {"error": str(exc)}, 400

    with borrow_mqtt() as mqtt:
        try:
            current = mqtt.refresh_z_offset(timeout=Z_OFFSET_REFRESH_TIMEOUT_S)
            target_mm = round(current["mm"] + delta_mm, 2)
            result = _set_printer_z_offset(mqtt, target_mm, current=current)
            result["nudge"] = {
                "mm": delta_mm,
                "display": f"{delta_mm:+.2f} mm",
            }
            return result
        except TimeoutError as exc:
            return {"error": str(exc)}, 504


def _read_bed_leveling_grid():
    """Read the bilinear bed leveling grid from the printer via M420 V.

    Opens a short-lived MQTT connection, sends M420 V with a 4-second
    drain window, parses BL-Grid lines from the combined response, and
    returns the grid as a 2-D JSON array together with min/max statistics.

    Returns:
        (data_dict, None) on success where data_dict contains grid/min/max/rows/cols.
        (None, (error_dict, http_status)) on failure.
    """
    import re
    import cli.mqtt as cli_mqtt

    config = app.config.get("config")
    if not config:
        return None, ({"error": "No configuration loaded"}, 503)

    with config.open() as cfg:
        if not cfg:
            return None, ({"error": "No printers configured"}, 503)

    printer_index = app.config.get("printer_index", 0)
    insecure = app.config.get("insecure", False)

    try:
        client = cli_mqtt.mqtt_open(config, printer_index, insecure)
    except Exception as exc:
        log.warning(f"bed-leveling: MQTT connect failed: {exc}")
        return None, ({"error": f"MQTT connection failed: {exc}"}, 503)

    try:
        msgs = cli_mqtt.mqtt_gcode_dump(client, "M420 V", collect_window=4.0)
    except Exception as exc:
        log.warning(f"bed-leveling: gcode dump failed: {exc}")
        return None, ({"error": f"GCode dump failed: {exc}"}, 503)

    # Combine all resData fields into one text block for parsing
    combined = "\n".join(
        msg.get("resData", "") for msg in msgs if isinstance(msg, dict)
    )

    # Parse lines like: " BL-Grid-0 -0.767 -0.642 ..."
    bl_pattern = re.compile(r"BL-Grid-\d+\s+([-\d.\s]+)")
    grid = []
    for line in combined.splitlines():
        match = bl_pattern.search(line)
        if match:
            values = [float(v) for v in match.group(1).split() if v]
            if values:
                grid.append(values)

    if not grid:
        log.warning("bed-leveling: no BL-Grid data found in MQTT response")
        return None, ({"error": "No bed leveling data received from printer"}, 504)

    all_values = [v for row in grid for v in row]
    data = {
        "grid": grid,
        "min": min(all_values),
        "max": max(all_values),
        "rows": len(grid),
        "cols": max(len(row) for row in grid),
    }

    # Persist grid to log directory as a timestamped .bed file
    if _log_dir:
        bed_dir = os.path.join(_log_dir, "bed_leveling")
        try:
            from datetime import datetime
            os.makedirs(bed_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bed_path = os.path.join(bed_dir, f"{ts}.bed")
            with open(bed_path, "w") as f:
                json.dump(data, f)
            log.info(f"bed-leveling: saved grid to {bed_path}")
        except Exception as exc:
            log.warning(f"bed-leveling: could not save grid: {exc}")

    return data, None


_PRINTER_REPORT_COMMANDS = {
    "firmware": {
        "label": "Firmware / Capabilities",
        "gcode": "M115",
        "window": 2.0,
        "drain": 2,
    },
    "settings": {
        "label": "Stored Settings",
        "gcode": "M503",
        "window": 4.0,
        "drain": 8,
    },
    "probe_offset": {
        "label": "Probe Offset",
        "gcode": "M851",
        "window": 2.0,
        "drain": 2,
    },
    "babystep": {
        "label": "Babystep / Z-Offset",
        "gcode": "M290 R",
        "window": 2.0,
        "drain": 2,
    },
    "bed_mesh": {
        "label": "Bed Mesh",
        "gcode": "M420 V",
        "window": 4.0,
        "drain": 6,
    },
}


_SUMMARY_COMMAND_GROUPS = {
    "leveling": ("M851", "M420", "M290"),
    "motion": ("M201", "M203", "M204", "M205", "M206"),
    "thermal": ("M301", "M145"),
    "motors": ("M907",),
    "tooling": ("M218",),
}


def _disconnect_mqtt_client(client):
    mqtt_client = getattr(client, "_mqtt", None)
    if mqtt_client is None:
        return
    try:
        mqtt_client.disconnect()
    except Exception as exc:
        log.debug(f"MQTT client disconnect failed: {exc}")


def _probe_printer_storage_files(source="onboard", source_value=None, timeout=5.0, collect_window=1.0):
    config = app.config.get("config")
    if not config:
        return None, ({"error": "No configuration loaded"}, 503)

    with config.open() as cfg:
        if not cfg:
            return None, ({"error": "No printers configured"}, 503)

    try:
        source_value = cli.mqtt.mqtt_file_list_source_value(source=source, value=source_value)
    except (TypeError, ValueError):
        return None, ({"error": "Invalid storage source"}, 400)

    printer_index = app.config.get("printer_index", 0)
    insecure = app.config.get("insecure", False)

    client = None
    try:
        client = cli.mqtt.mqtt_open(config, printer_index, insecure)
        result = cli.mqtt.mqtt_file_list_probe(
            client,
            source=source,
            source_value=source_value,
            timeout=timeout,
            collect_window=collect_window,
        )
    except Exception as exc:
        log.warning(f"storage-file-list: MQTT probe failed: {exc}")
        return None, ({"error": f"MQTT storage probe failed: {exc}"}, 503)
    finally:
        if client is not None:
            _disconnect_mqtt_client(client)

    if not result.get("replies"):
        return None, ({"error": f"No response from printer for storage source '{source}'"}, 504)

    return result, None


def _validate_printer_storage_path(file_path, source=None):
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("Stored file path is required")

    normalized_path = file_path.strip()
    inferred_source = cli.mqtt.infer_storage_source_from_path(normalized_path)
    if inferred_source not in {"onboard", "usb"}:
        raise ValueError("Unsupported stored file path")

    if source is not None and inferred_source != source:
        raise ValueError(f"Stored file path does not match source '{source}'")

    return normalized_path, inferred_source


def _fetch_remote_image(preview_url, timeout=10.0):
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    preview_url = str(preview_url or "").strip()
    if not preview_url.startswith(("http://", "https://")):
        raise ValueError("Invalid preview URL")

    request_obj = Request(preview_url, headers={"User-Agent": "ankerctl"})
    try:
        with urlopen(request_obj, timeout=timeout) as remote:
            data = remote.read()
            content_type = remote.headers.get_content_type() if remote.headers else None
            return data, (content_type or "image/jpeg")
    except HTTPError as exc:
        raise RuntimeError(f"Preview image request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Preview image request failed: {exc.reason}") from exc


def _proxy_preview_image_response(preview_url):
    try:
        data, content_type = _fetch_remote_image(preview_url)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except RuntimeError as exc:
        return {"error": str(exc)}, 502

    response = Response(data, mimetype=content_type)
    response.headers["Cache-Control"] = "private, max-age=600"
    return response


def _clean_printer_report_output(raw_output):
    import re

    if not raw_output:
        return ""

    text = re.sub(r"\x1b\[[0-9;]*m", "", raw_output).replace("\r", "\n")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "ok" or stripped.startswith("+ringbuf") or stripped == "+rin":
            continue
        if stripped.startswith("Unknown com") or (stripped.startswith("X:") and "Count" in stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines)


def _collect_printer_gcode_output(client, gcode, *, window, drain):
    import cli.mqtt as cli_mqtt

    chunks = []
    msgs = cli_mqtt.mqtt_gcode_dump(client, gcode, collect_window=window)
    if not msgs:
        raise TimeoutError(f"No response from printer for {gcode}")

    for msg in msgs:
        chunks.append(msg.get("resData", ""))

    for probe_num in range(max(0, int(drain))):
        time.sleep(0.3)
        probe_msgs = cli_mqtt.mqtt_gcode_dump(client, "M114", collect_window=1.0)
        if not probe_msgs:
            break
        chunk = probe_msgs[0].get("resData", "")
        chunks.append(chunk)
        ringbuf_pos = probe_msgs[0].get("resLen", 0)
        if probe_num > 0 and ringbuf_pos <= 64 and "echo:" not in chunk and "z1:" not in chunk:
            break

    raw_output = "".join(chunks)
    cleaned_output = _clean_printer_report_output(raw_output)
    return {
        "raw_output": raw_output,
        "cleaned_output": cleaned_output,
        "chunks": chunks,
        "chunk_count": len(chunks),
    }


def _read_printer_report(name):
    import cli.mqtt as cli_mqtt

    if name not in _PRINTER_REPORT_COMMANDS:
        raise KeyError(name)

    report = _PRINTER_REPORT_COMMANDS[name]
    config = app.config.get("config")
    if not config:
        raise ConnectionError("No configuration loaded")

    printer_index = app.config.get("printer_index", 0)
    insecure = app.config.get("insecure", False)

    try:
        client = cli_mqtt.mqtt_open(config, printer_index, insecure)
        output = _collect_printer_gcode_output(
            client,
            report["gcode"],
            window=report["window"],
            drain=report["drain"],
        )
    finally:
        if "client" in locals():
            _disconnect_mqtt_client(client)

    return {
        "name": name,
        "label": report["label"],
        "gcode": report["gcode"],
        **output,
    }


def _extract_report_commands(*texts):
    import re

    pattern = re.compile(
        r"(M(?:92|145|201|203|204|205|206|218|290|301|420|425|665|851|907)\b[^\r\n+]*)"
    )

    commands = {}
    for text in texts:
        if not text:
            continue
        for match in pattern.findall(text):
            command = match.strip()
            prefix = command.split()[0]
            commands.setdefault(prefix, command)
    return commands


def _build_command_group(commands, keys):
    return [
        {"command": key, "value": commands[key]}
        for key in keys
        if key in commands
    ]


def _read_printer_settings_summary():
    with borrow_mqtt() as mqtt:
        live_z_offset = mqtt.get_z_offset_state()
        if not live_z_offset.get("available"):
            try:
                live_z_offset = mqtt.refresh_z_offset(timeout=Z_OFFSET_REFRESH_TIMEOUT_S)
            except TimeoutError:
                live_z_offset = mqtt.get_z_offset_state()

    reports = {}
    for name in ("settings", "probe_offset", "babystep"):
        try:
            reports[name] = _read_printer_report(name)
        except Exception as exc:
            reports[name] = {"name": name, "error": str(exc)}

    commands = _extract_report_commands(
        reports.get("settings", {}).get("cleaned_output", ""),
        reports.get("probe_offset", {}).get("cleaned_output", ""),
        reports.get("babystep", {}).get("cleaned_output", ""),
    )

    highlights = []
    if live_z_offset.get("available"):
        highlights.append({
            "label": "Live Z-Offset",
            "command": "MQTT 1021",
            "value": f"{live_z_offset['mm']:.2f} mm",
        })
    if "M851" in commands:
        highlights.append({
            "label": "Stored Probe Offset",
            "command": "M851",
            "value": commands["M851"],
        })
    if "M420" in commands:
        highlights.append({
            "label": "Bed Leveling",
            "command": "M420",
            "value": commands["M420"],
        })
    if "M301" in commands:
        highlights.append({
            "label": "Hotend PID",
            "command": "M301",
            "value": commands["M301"],
        })

    groups = {
        name: _build_command_group(commands, keys)
        for name, keys in _SUMMARY_COMMAND_GROUPS.items()
    }

    return {
        "status": "ok",
        "live_z_offset": _serialize_z_offset_state(live_z_offset),
        "highlights": highlights,
        "groups": groups,
        "reports": {
            key: {
                "name": value.get("name", key),
                "label": value.get("label"),
                "gcode": value.get("gcode"),
                "available": "error" not in value,
                "error": value.get("error"),
            }
            for key, value in reports.items()
        },
    }


@app.get("/api/printer/bed-leveling")
def app_api_printer_bed_leveling():
    """Read the bilinear bed leveling grid from the printer.

    Opens a short-lived MQTT connection, sends M420 V, parses the BL-Grid
    response and returns the grid with statistics. Takes up to ~15 seconds.
    Do not call this during an active print.
    """
    data, err = _read_bed_leveling_grid()
    if err is not None:
        return err
    return data


@app.get("/api/printer/settings-summary")
def app_api_printer_settings_summary():
    try:
        return _read_printer_settings_summary()
    except TimeoutError as exc:
        return {"error": str(exc)}, 504
    except ConnectionError as exc:
        return {"error": str(exc)}, 503


@app.get("/api/printer/runtime-state")
def app_api_printer_runtime_state():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", **state}


@app.get("/api/printer/alerts")
def app_api_printer_alerts():
    buffer = _get_printer_alert_buffer()
    limit = request.args.get("limit", 20, type=int)
    after = request.args.get("after", None, type=int)
    return buffer.snapshot(limit=limit, after_id=after)


@app.get("/api/printer/bed-leveling/last")
def app_api_printer_bed_leveling_last():
    """Return the most recently saved bed leveling grid from the log directory."""
    import glob
    if not _log_dir:
        return {"error": "No log directory configured (set ANKERCTL_LOG_DIR)"}, 404
    bed_dir = os.path.join(_log_dir, "bed_leveling")
    files = sorted(glob.glob(os.path.join(bed_dir, "*.bed")))
    if not files:
        return {"error": "No saved bed leveling data found"}, 404
    with open(files[-1]) as f:
        data = json.load(f)
    data["saved_at"] = os.path.basename(files[-1]).replace(".bed", "")
    return data


def _local_web_host_port():
    host = os.getenv("FLASK_HOST") or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = os.getenv("FLASK_PORT") or str(app.config.get("port") or "4470")
    return host, port


def _validate_selected_printer_camera(camera_settings, *, stream_state=True):
    if camera_settings.get("effective_source") != web.camera.CAMERA_SOURCE_PRINTER:
        return None

    printer_index = camera_settings.get("printer_index", app.config.get("printer_index", 0))
    if not _printer_video_supported(printer_index=printer_index):
        return {"error": "Printer camera is not supported for the selected printer"}, 400

    if not stream_state:
        return None

    vq = get_video_service(printer_index)
    if not vq:
        return {"error": "Video service not available"}, 503
    if not getattr(vq, "video_enabled", False):
        return {"error": "Enable printer video before taking a snapshot"}, 409
    if not _video_has_recent_frame(vq):
        return {
            "error": "Printer video is enabled, but no live camera frames are available yet. Wait for live video to appear and try again."
        }, 409
    return None


def _capture_selected_camera_snapshot_temp(camera_settings, *, scale=None, for_timelapse=False):
    camera_error = _validate_selected_printer_camera(camera_settings, stream_state=False)
    if camera_error is not None:
        raise ValueError(camera_error)

    ffmpeg_path = _ffmpeg_path()
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg not installed")

    camera_error = _validate_selected_printer_camera(camera_settings, stream_state=True)
    if camera_error is not None:
        raise ValueError(camera_error)

    temp_path = web.camera.create_temp_snapshot_file()
    host, port = _local_web_host_port()
    web.camera.capture_camera_snapshot_to_file(
        camera_settings,
        ffmpeg_path,
        temp_path,
        host=host,
        port=port,
        api_key=app.config.get("api_key"),
        timeout=SNAPSHOT_FFMPEG_TIMEOUT_SEC,
        for_timelapse=for_timelapse,
        scale=scale,
    )
    return temp_path


@app.get("/api/camera/frame")
def app_api_camera_frame():
    """Return a current frame from the selected camera as an inline JPEG."""
    from flask import after_this_request, send_file

    camera_settings = _resolve_camera_settings(printer_index=_requested_printer_index())
    if not camera_settings.get("effective_source"):
        return {"error": camera_settings.get("detail") or "No camera source is available"}, 400

    try:
        temp_path = _capture_selected_camera_snapshot_temp(camera_settings, scale=(1280, 720))
    except RuntimeError as exc:
        return {"error": str(exc)}, 500
    except ValueError as exc:
        payload, status = exc.args[0]
        return payload, status
    except web.camera.CameraCaptureError as exc:
        return {"error": str(exc)}, 502
    except subprocess.TimeoutExpired:
        return {"error": "Camera frame timed out waiting for a response."}, 504
    except OSError as exc:
        return {"error": f"Camera frame capture failed: {exc}"}, 500

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return response

    return send_file(temp_path, mimetype="image/jpeg", as_attachment=False)


@app.get("/api/snapshot")
def app_api_snapshot():
    """Capture a JPEG snapshot from the camera and return it as a file download."""
    import subprocess
    from datetime import datetime
    from flask import after_this_request, send_file

    printer_index = _requested_printer_index()
    camera_settings = _resolve_camera_settings(printer_index=printer_index)
    if not camera_settings.get("effective_source"):
        return {"error": camera_settings.get("detail") or "No camera source is available"}, 400

    try:
        temp_path = _capture_selected_camera_snapshot_temp(camera_settings)
    except RuntimeError as exc:
        return {"error": str(exc)}, 500
    except ValueError as exc:
        payload, status = exc.args[0]
        return payload, status
    except web.camera.CameraCaptureError as exc:
        return {"error": f"Snapshot failed: {exc}"}, 500
    except subprocess.TimeoutExpired:
        return {"error": "Snapshot timed out waiting for a camera frame. Wait for the camera to respond and try again."}, 504
    except OSError as err:
        return {"error": f"Snapshot could not run ffmpeg: {err}"}, 500

    taken_at = datetime.now()
    with borrow_mqtt(printer_index) as mqtt:
        timelapse = getattr(mqtt, "timelapse", None) if mqtt else None
        if timelapse:
            try:
                timelapse.save_manual_snapshot(
                    temp_path,
                    camera_settings=camera_settings,
                    taken_at=taken_at,
                )
            except OSError as err:
                log.warning(f"Manual snapshot archive save failed: {err}")

    @after_this_request
    def _cleanup(response):
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return response

    timestamp = taken_at.strftime("%Y%m%d_%H%M%S")
    return send_file(
        temp_path,
        mimetype="image/jpeg",
        as_attachment=True,
        download_name=f"ankerctl_snapshot_{timestamp}.jpg",
    )

@app.get("/api/history")
def app_api_history():
    """Return print history as JSON with pagination."""
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    # Clamp parameters to safe ranges to prevent excessive queries or errors
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"entries": [], "total": 0}
        entries = mqtt.history.get_history(limit=limit, offset=offset)
        total = mqtt.history.get_count()
    serialized_entries = []
    for entry in entries:
        item = dict(entry)
        item["thumbnail_url"] = (
            url_for("app_api_history_thumbnail", entry_id=item["id"])
            if item.get("thumbnail_available")
            else None
        )
        serialized_entries.append(item)
    return {"entries": serialized_entries, "total": total}


@app.delete("/api/history")
def app_api_history_clear():
    """Clear all print history."""
    with borrow_mqtt() as mqtt:
        mqtt.history.clear()
    return {"status": "ok"}


@app.post("/api/history/delete")
def app_api_history_delete_selected():
    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, list):
        return {"error": "ids must be a list of history entry ids"}, 400

    entry_ids = []
    for raw_id in raw_ids:
        try:
            entry_id = int(raw_id)
        except (TypeError, ValueError):
            return {"error": "ids must contain integers"}, 400
        if entry_id > 0 and entry_id not in entry_ids:
            entry_ids.append(entry_id)

    if not entry_ids:
        return {"error": "No history entries were selected"}, 400

    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        entries = [mqtt.history.get_entry(entry_id) for entry_id in entry_ids]
        active = [entry for entry in entries if entry and entry.get("status") == "started"]
        if active:
            return {"error": "Cannot delete an in-progress history entry"}, 409
        deleted = mqtt.history.delete_entries(entry_ids)

    return {
        "status": "ok",
        "deleted": deleted,
        "requested": len(entry_ids),
    }


@app.get("/api/history/<int:entry_id>/thumbnail")
def app_api_history_thumbnail(entry_id):
    from flask import send_file

    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        entry = mqtt.history.get_entry(entry_id)
        if not entry:
            return {"error": "History entry not found"}, 404
        thumbnail_path = mqtt.history.get_thumbnail_path(entry_id)
        preview_url = entry.get("preview_url")

    if thumbnail_path:
        response = send_file(
            thumbnail_path,
            mimetype="image/png",
            as_attachment=False,
            download_name=os.path.basename(thumbnail_path),
        )
        response.headers["Cache-Control"] = "private, max-age=600"
        return response

    if preview_url:
        return _proxy_preview_image_response(preview_url)

    return {"error": "Thumbnail not available for this history entry"}, 404


@app.post("/api/history/<int:entry_id>/reprint")
def app_api_history_reprint(entry_id):
    user_name = request.headers.get("User-Agent", "ankerctl").split(url_for('app_root'))[0]
    printer_index = app.config.get("printer_index", 0)

    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        if mqtt.is_printing or mqtt.has_pending_print_start or mqtt.is_preparing_print:
            return {"error": "Printer is already busy with another print job"}, 409
        entry = mqtt.history.get_entry(entry_id)
        if not entry:
            return {"error": "History entry not found"}, 404
        archive_path = mqtt.history.get_archive_path(entry_id)
        if not archive_path:
            return {"error": "No archived GCode is available for this history entry"}, 404

    with app.config["config"].open() as cfg:
        rate_limit_mbps, rate_limit_source = cli.util.resolve_upload_rate_mbps_with_source(cfg)

    with open(archive_path, "rb") as fh:
        archive_bytes = fh.read()

    with app.svc.borrow("filetransfer") as ft:
        if not ft:
            return {"error": "File transfer service unavailable"}, 503
        try:
            ft.send_bytes(
                archive_bytes,
                entry["filename"],
                user_name,
                rate_limit_mbps=rate_limit_mbps,
                start_print=True,
                printer_index=printer_index,
                archive_info={
                    "archive_relpath": entry.get("archive_relpath"),
                    "archive_size": entry.get("archive_size"),
                },
            )
        except ConnectionError as exc:
            log.error(f"History reprint connection error: {exc}")
            return {"error": str(exc)}, 503

    return {
        "status": "ok",
        "name": entry["filename"],
        "upload_rate_mbps": rate_limit_mbps,
        "upload_rate_source": rate_limit_source,
    }


@app.get("/api/filaments")
def app_api_filaments_list():
    """List all filament profiles."""
    return {"filaments": app.filaments.list_all()}


@app.post("/api/filaments")
def app_api_filaments_create():
    """Create a new filament profile."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "Invalid JSON payload"}, 400
    try:
        profile = app.filaments.create(data)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    return profile, 201


@app.put("/api/filaments/<int:profile_id>")
def app_api_filaments_update(profile_id):
    """Update an existing filament profile."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": "Invalid JSON payload"}, 400
    profile = app.filaments.update(profile_id, data)
    if profile is None:
        return {"error": "Profile not found"}, 404
    return profile


@app.delete("/api/filaments/<int:profile_id>")
def app_api_filaments_delete(profile_id):
    """Delete a filament profile."""
    deleted = app.filaments.delete(profile_id)
    if not deleted:
        return {"error": "Profile not found"}, 404
    return {"status": "ok"}


@app.post("/api/filaments/<int:profile_id>/apply")
def app_api_filaments_apply(profile_id):
    """Send M104/M140 GCode to the printer for the given filament profile."""
    profile = app.filaments.get(profile_id)
    if profile is None:
        return {"error": "Profile not found"}, 404
    nozzle = profile.get("nozzle_temp_first_layer") or profile.get("nozzle_temp_other_layer") or profile.get("nozzle_temp", 0)
    bed    = profile.get("bed_temp_first_layer") or profile.get("bed_temp_other_layer") or profile.get("bed_temp", 0)
    try:
        nozzle = max(0, min(int(nozzle), 350))
        bed    = max(0, min(int(bed), 130))
    except (TypeError, ValueError):
        return {"error": "Invalid temperature values in filament profile"}, 422
    gcode = f"M104 S{nozzle}\nM140 S{bed}"
    with borrow_mqtt() as mqtt:
        mqtt.send_gcode(gcode)
    return {"status": "ok", "gcode": gcode}


@app.post("/api/filaments/<int:profile_id>/duplicate")
def app_api_filaments_duplicate(profile_id):
    """Duplicate a filament profile."""
    profile = app.filaments.duplicate(profile_id)
    if profile is None:
        return {"error": "Profile not found"}, 404
    return profile, 201


@app.get("/api/filaments/service/swap")
def app_api_filament_service_swap_state():
    with app.filament_swap_lock:
        return _serialize_filament_swap_state(app.filament_swap_state)


@app.post("/api/filaments/service/preheat")
def app_api_filament_service_preheat():
    payload = request.get_json(silent=True) or {}
    try:
        profile = _filament_service_profile(payload, "profile_id")
        temp_c = _filament_service_temp(profile)
        gcode = f"M104 S{temp_c}"
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
            mqtt.send_gcode(gcode)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except LookupError as exc:
        return {"error": str(exc)}, 404
    except RuntimeError as exc:
        return {"error": str(exc)}, 409
    except ConnectionError as exc:
        return {"error": str(exc)}, 503

    return {
        "status": "ok",
        "action": "preheat",
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "target_temp_c": temp_c,
        "gcode": gcode,
    }


@app.post("/api/filaments/service/move")
def app_api_filament_service_move():
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    if action not in {"extrude", "retract"}:
        return {"error": "action must be 'extrude' or 'retract'"}, 400

    try:
        config = app.config["config"]
        with config.open() as cfg:
            filament_settings = _normalize_filament_service_settings(
                _resolve_filament_service_settings(cfg) if cfg else cli.model.default_filament_service_config()
            )
        profile = _filament_service_profile(payload, "profile_id")
        temp_c = _filament_service_temp(profile)
        raw_length_mm = payload.get("length_mm", filament_settings["quick_move_length_mm"])
        length_mm = _filament_service_length({"length_mm": raw_length_mm}, "length_mm")
        delta_mm = length_mm if action == "extrude" else -length_mm
        feedrate_mm_min = (
            FILAMENT_SERVICE_EXTRUDE_FEEDRATE_MM_MIN
            if action == "extrude"
            else FILAMENT_SERVICE_RETRACT_FEEDRATE_MM_MIN
        )
        gcode = _build_filament_move_gcode(
            delta_mm,
            feedrate_mm_min=feedrate_mm_min,
        )
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
            current_temp = mqtt.nozzle_temp
            wait_for_heat = current_temp is None or current_temp < (temp_c - FILAMENT_SERVICE_HEAT_TOLERANCE_C)
            if wait_for_heat:
                mqtt.send_gcode(f"M104 S{temp_c}")
                current_temp = _wait_for_filament_service_nozzle(mqtt, temp_c)
            mqtt.send_gcode(gcode)
    except ValueError as exc:
        return {"error": str(exc)}, 400
    except LookupError as exc:
        return {"error": str(exc)}, 404
    except RuntimeError as exc:
        return {"error": str(exc)}, 409
    except TimeoutError as exc:
        return {"error": str(exc)}, 504
    except ConnectionError as exc:
        return {"error": str(exc)}, 503

    return {
        "status": "ok",
        "action": action,
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "target_temp_c": temp_c,
        "current_temp_c": current_temp,
        "length_mm": length_mm,
        "gcode": gcode,
    }


@app.post("/api/filaments/service/swap/start")
def app_api_filament_service_swap_start():
    payload = request.get_json(silent=True) or {}

    config = app.config["config"]
    with config.open() as cfg:
        if not cfg:
            return {"error": "No printers configured"}, 400
        filament_settings = _normalize_filament_service_settings(_resolve_filament_service_settings(cfg))

    allow_legacy_swap = bool(filament_settings.get("allow_legacy_swap"))
    manual_swap_preheat_temp_c = _filament_service_manual_swap_temp(filament_settings)

    unload_profile = None
    load_profile = None
    unload_temp_c = manual_swap_preheat_temp_c
    load_temp_c = manual_swap_preheat_temp_c
    unload_length_mm = 0.0
    load_length_mm = 0.0
    unload_feedrate_mm_min = FILAMENT_SERVICE_FEEDRATE_MM_MIN
    load_feedrate_mm_min = FILAMENT_SERVICE_FEEDRATE_MM_MIN

    if allow_legacy_swap:
        try:
            unload_profile = _filament_service_profile(payload, "unload_profile_id")
            load_profile = _filament_service_profile(payload, "load_profile_id")
            unload_temp_c = _filament_service_temp(unload_profile)
            load_temp_c = _filament_service_temp(load_profile)
            unload_length_mm = _filament_service_length(
                {"unload_length_mm": payload.get("unload_length_mm", filament_settings["swap_unload_length_mm"])},
                "unload_length_mm",
            )
            load_length_mm = _filament_service_length(
                {"load_length_mm": payload.get("load_length_mm", filament_settings["swap_load_length_mm"])},
                "load_length_mm",
            )
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except LookupError as exc:
            return {"error": str(exc)}, 404

        unload_feedrate_mm_min = FILAMENT_SERVICE_SWAP_UNLOAD_FEEDRATE_MM_MIN
        load_feedrate_mm_min = FILAMENT_SERVICE_SWAP_LOAD_FEEDRATE_MM_MIN

    with app.filament_swap_lock:
        if app.filament_swap_state is not None:
            return {"error": "A filament swap is already in progress"}, 409

    swap_state = {
        "token": token(12),
        "created_at": int(time.time()),
        "mode": "legacy" if allow_legacy_swap else "manual",
        "phase": "heating_unload" if allow_legacy_swap else "await_manual_swap",
        "message": None,
        "error": None,
        "unload_profile_id": unload_profile["id"] if unload_profile else None,
        "unload_profile_name": unload_profile["name"] if unload_profile else None,
        "load_profile_id": load_profile["id"] if load_profile else None,
        "load_profile_name": load_profile["name"] if load_profile else None,
        "unload_temp_c": unload_temp_c,
        "load_temp_c": load_temp_c,
        "unload_length_mm": unload_length_mm,
        "load_length_mm": load_length_mm,
        "manual_swap_preheat_temp_c": manual_swap_preheat_temp_c,
        "unload_feedrate_mm_min": unload_feedrate_mm_min,
        "load_feedrate_mm_min": load_feedrate_mm_min,
    }

    if allow_legacy_swap:
        swap_state["message"] = (
            f"Heating for automatic unload of {unload_profile['name']} "
            f"at {unload_temp_c}°C."
        )
    else:
        swap_state["message"] = (
            f"Recommended method enabled: preheating nozzle to {manual_swap_preheat_temp_c}°C. "
            "Release the extruder lever, remove the filament manually, insert the new filament, "
            "then confirm. Use Quick Extrude afterward if you need to purge."
        )

    with app.filament_swap_lock:
        app.filament_swap_state = swap_state

    try:
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
            if allow_legacy_swap:
                _filament_swap_start_background(_run_legacy_swap_unload, swap_state["token"])
            else:
                mqtt.send_gcode(f"M104 S{manual_swap_preheat_temp_c}")
    except RuntimeError as exc:
        _filament_swap_state_clear(swap_state["token"])
        return {"error": str(exc)}, 409
    except TimeoutError as exc:
        _filament_swap_state_clear(swap_state["token"])
        return {"error": str(exc)}, 504
    except ConnectionError as exc:
        _filament_swap_state_clear(swap_state["token"])
        return {"error": str(exc)}, 503

    return {
        "status": "ok",
        "message": swap_state["message"],
        "gcode": f"M104 S{manual_swap_preheat_temp_c}" if not allow_legacy_swap else None,
        **_serialize_filament_swap_state(swap_state),
    }


@app.post("/api/filaments/service/swap/confirm")
def app_api_filament_service_swap_confirm():
    payload = request.get_json(silent=True) or {}
    with app.filament_swap_lock:
        swap_state = app.filament_swap_state
        if swap_state is None:
            return {"error": "No filament swap is in progress"}, 409
        provided_token = payload.get("token")
        if provided_token and provided_token != swap_state["token"]:
            return {"error": "Swap token mismatch"}, 409
        if swap_state.get("phase") in {"heating_unload", "unloading", "heating_load", "loading"}:
            return {"error": "Swap stage is still running; wait for it to finish first"}, 409

    if swap_state.get("mode") == "manual":
        completed_swap = _filament_swap_state_clear(swap_state["token"])
        return {
            "status": "ok",
            "message": (
                "Manual swap marked complete. If needed, use Quick Extrude to prime "
                "the new filament."
            ),
            "completed_swap": completed_swap,
            "pending": False,
            "swap": None,
        }

    _filament_swap_state_update(
        swap_state["token"],
        phase="heating_load",
        message=(
            f"Heating for automatic load / purge of {swap_state['load_profile_name']} "
            f"at {swap_state['load_temp_c']}°C."
        ),
        error=None,
    )
    try:
        with borrow_mqtt() as mqtt:
            _assert_filament_service_ready(mqtt)
    except RuntimeError as exc:
        _filament_swap_state_update(swap_state["token"], phase="error", message=str(exc), error=str(exc))
        return {"error": str(exc)}, 409
    except ConnectionError as exc:
        _filament_swap_state_update(swap_state["token"], phase="error", message=str(exc), error=str(exc))
        return {"error": str(exc)}, 503

    _filament_swap_start_background(_run_legacy_swap_load, swap_state["token"])
    current_state = _filament_swap_state_get(swap_state["token"])
    return {
        "status": "ok",
        "message": current_state["message"],
        **_serialize_filament_swap_state(current_state),
    }


@app.post("/api/filaments/service/swap/cancel")
def app_api_filament_service_swap_cancel():
    payload = request.get_json(silent=True) or {}
    with app.filament_swap_lock:
        swap_state = app.filament_swap_state
        if swap_state is None:
            return {"status": "ok", "pending": False, "swap": None}
        provided_token = payload.get("token")
        if provided_token and provided_token != swap_state["token"]:
            return {"error": "Swap token mismatch"}, 409
        if swap_state.get("phase") in {"heating_unload", "unloading", "heating_load", "loading"}:
            return {"error": "Cannot cancel while an automatic swap stage is running"}, 409
        app.filament_swap_state = None

    return {
        "status": "ok",
        "message": "Filament swap cancelled.",
        "cancelled_swap": swap_state,
        "pending": False,
        "swap": None,
    }


@app.get("/api/timelapses")
def app_api_timelapses():
    """List available timelapse videos."""
    with borrow_mqtt() as mqtt:
        videos = mqtt.timelapse.list_videos()
        enabled = mqtt.timelapse.enabled
    return {"videos": videos, "enabled": enabled}


@app.get("/api/timelapse-snapshots")
def app_api_timelapse_snapshots():
    """List available timelapse snapshot collections and frames."""
    with borrow_mqtt() as mqtt:
        collections = mqtt.timelapse.list_snapshots()
        enabled = mqtt.timelapse.enabled
    return {"collections": collections, "enabled": enabled}


@app.post("/api/timelapse/current/start")
def app_api_timelapse_current_start():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        try:
            filename = mqtt.start_timelapse_for_current_print()
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", "filename": filename, **state}


@app.post("/api/timelapse/current/dismiss")
def app_api_timelapse_current_dismiss():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        mqtt.dismiss_timelapse_start_offer()
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", **state}


@app.post("/api/timelapse/current/pause")
def app_api_timelapse_current_pause():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        try:
            filename = mqtt.pause_timelapse_for_current_print()
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", "filename": filename, **state}


@app.post("/api/timelapse/current/resume")
def app_api_timelapse_current_resume():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        try:
            filename = mqtt.resume_timelapse_for_current_print()
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", "filename": filename, **state}


@app.post("/api/timelapse/current/stop")
def app_api_timelapse_current_stop():
    with borrow_mqtt() as mqtt:
        if not mqtt:
            return {"error": "Service unavailable"}, 503
        try:
            filename = mqtt.stop_timelapse_for_current_print()
        except RuntimeError as exc:
            return {"error": str(exc)}, 409
        state = _build_runtime_state_payload(mqtt)
    return {"status": "ok", "filename": filename, **state}


@app.get("/api/timelapse/<filename>")
def app_api_timelapse_download(filename):
    """Download a timelapse video."""
    from flask import send_file
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "invalid filename"}), 400
    with borrow_mqtt() as mqtt:
        path = mqtt.timelapse.get_video_path(filename)
        captures_dir = os.path.realpath(mqtt.timelapse._captures_dir)
    if not path:
        return {"error": "Video not found"}, 404
    if not os.path.realpath(path).startswith(captures_dir + os.sep):
        return jsonify({"error": "invalid filename"}), 400
    return send_file(path, mimetype="video/mp4", as_attachment=False, download_name=filename)


@app.delete("/api/timelapse/<filename>")
def app_api_timelapse_delete(filename):
    """Delete a timelapse video."""
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "invalid filename"}), 400
    with borrow_mqtt() as mqtt:
        captures_dir = os.path.realpath(mqtt.timelapse._captures_dir)
        path = mqtt.timelapse.get_video_path(filename)
        if not path or not os.path.realpath(path).startswith(captures_dir + os.sep):
            return {"error": "Video not found"}, 404
        deleted = mqtt.timelapse.delete_video(filename)
    if not deleted:
        return {"error": "Video not found"}, 404
    return {"status": "ok"}


@app.get("/api/timelapse-snapshot/<collection_id>/<filename>")
def app_api_timelapse_snapshot_download(collection_id, filename):
    """Return a timelapse snapshot JPG for preview or download."""
    from flask import send_file

    if (
        "/" in collection_id or "\\" in collection_id or ".." in collection_id
        or "/" in filename or "\\" in filename or ".." in filename
    ):
        return jsonify({"error": "invalid filename"}), 400

    with borrow_mqtt() as mqtt:
        path = mqtt.timelapse.get_snapshot_path(collection_id, filename)
        captures_dir = os.path.realpath(mqtt.timelapse._captures_dir)

    if not path:
        return {"error": "Snapshot not found"}, 404
    if not os.path.realpath(path).startswith(captures_dir + os.sep):
        return jsonify({"error": "invalid filename"}), 400

    download = request.args.get("download")
    return send_file(
        path,
        mimetype="image/jpeg",
        as_attachment=download in {"1", "true", "yes"},
        download_name=filename,
    )


@app.delete("/api/timelapse-snapshot/<collection_id>/<filename>")
def app_api_timelapse_snapshot_delete(collection_id, filename):
    """Delete an archived timelapse snapshot JPG."""
    if (
        "/" in collection_id or "\\" in collection_id or ".." in collection_id
        or "/" in filename or "\\" in filename or ".." in filename
    ):
        return jsonify({"error": "invalid filename"}), 400

    with borrow_mqtt() as mqtt:
        try:
            deleted = mqtt.timelapse.delete_snapshot(collection_id, filename)
        except RuntimeError as exc:
            return {"error": str(exc)}, 409

    if not deleted:
        return {"error": "Snapshot not found"}, 404
    return {"status": "ok"}


def register_services(app):
    config = app.config.get("config")
    if not config:
        return

    with config.open() as cfg:
        if not cfg:
            return

        supported_indexes = []
        camera_supported_indexes = []
        for index, printer in enumerate(getattr(cfg, "printers", [])):
            if printer.model in UNSUPPORTED_PRINTERS:
                continue
            supported_indexes.append(index)
            if printer.model not in PRINTERS_WITHOUT_CAMERA:
                camera_supported_indexes.append(index)

    wanted_mqtt_services = {mqtt_service_name(index) for index in supported_indexes}
    wanted_video_services = {video_service_name(index) for index in camera_supported_indexes}
    wanted_pppp_services = {pppp_service_name(index) for index in camera_supported_indexes}
    for name, svc in list(iter_mqtt_services()):
        if name in wanted_mqtt_services:
            continue
        if app.svc.refs.get(name, 0) > 0:
            # Active WebSocket handlers are still holding references. Stopping the service
            # now would close the MQTT connection under them. Skip and retry on next reload.
            log.warning(f"Skipping stop of MQTT service {name!r}: {app.svc.refs[name]} active reference(s); will retry on next reload")
            continue
        svc.stop()
        try:
            svc.await_stopped()
        except Exception as exc:
            log.debug(f"Service {name} stop wait failed: {exc}")
        app.svc.unregister(name)

    for prefix, legacy_name, wanted_names, label in (
        (VIDEO_SERVICE_PREFIX, LEGACY_VIDEO_SERVICE_NAME, wanted_video_services, "video"),
        (PPPP_SERVICE_PREFIX, LEGACY_PPPP_SERVICE_NAME, wanted_pppp_services, "PPPP"),
    ):
        for name, svc in list(getattr(app.svc, "svcs", {}).items()):
            if not (name.startswith(prefix) or name == legacy_name):
                continue
            if name in wanted_names:
                continue
            if app.svc.refs.get(name, 0) > 0:
                log.warning(
                    f"Skipping stop of {label} service {name!r}: "
                    f"{app.svc.refs[name]} active reference(s); will retry on next reload"
                )
                continue
            svc.stop()
            try:
                svc.await_stopped()
            except Exception as exc:
                log.debug(f"Service {name} stop wait failed: {exc}")
            app.svc.unregister(name)

    if not supported_indexes:
        return

    if "filetransfer" not in app.svc:
        app.svc.register("filetransfer", web.service.filetransfer.FileTransferService())

    for printer_index in camera_supported_indexes:
        pppp_name = pppp_service_name(printer_index)
        if pppp_name not in app.svc:
            app.svc.register(pppp_name, web.service.pppp.PPPPService(printer_index=printer_index))

        video_name = video_service_name(printer_index)
        if video_name not in app.svc:
            app.svc.register(video_name, web.service.video.VideoQueue(printer_index=printer_index))

    for printer_index in supported_indexes:
        name = mqtt_service_name(printer_index)
        if name in app.svc:
            continue
        svc = web.service.mqtt.MqttQueue(printer_index=printer_index)
        app.svc.register(name, svc)
        svc.start()


@app.get("/api/console/logs")
def app_api_console_logs():
    buffer = _get_console_log_buffer()
    limit = max(1, min(request.args.get("limit", 200, type=int), 1000))
    after_id = request.args.get("after", default=None, type=int)
    return buffer.snapshot(limit=limit, after_id=after_id)


def webserver(config, printer_index, host, port, insecure=False, **kwargs):
    """
    Starts the Flask webserver

    Args:
        - config: A configuration object containing configuration information
        - host: A string containing host address to start the server
        - port: An integer specifying the port number of server
        - **kwargs: A dictionary containing additional configuration information

    Returns:
        - None
    """
    _get_console_log_buffer()

    # Filament profile store — initialized once at startup
    config_root = config.config_root
    app.filaments = FilamentStore(db_path=str(config_root / "filament.db"))

    # Ensure a stable Flask secret key that survives container restarts.
    # FLASK_SECRET_KEY env var takes precedence (set at import via from_prefixed_env).
    # Without the env var, the import-time key is a random ephemeral token that
    # changes on every restart.  Replace it with a value persisted in the config dir.
    if not os.getenv("FLASK_SECRET_KEY"):
        secret_key_path = config_root / "flask_secret.key"
        if not secret_key_path.exists():
            secret_key_path.write_text(token(24))
            secret_key_path.chmod(0o600)
        persisted = secret_key_path.read_text().strip()
        app.secret_key = persisted or token(24)

    # Resolve API key: ENV var takes precedence over config file
    api_key = cli.config.resolve_api_key(config)
    app.config["api_key"] = api_key
    if api_key:
        log.info("API key authentication enabled")
    else:
        log.info("No API key set. Authentication disabled.")

    printer_index_locked = os.getenv("PRINTER_INDEX") is not None
    app.config["printer_index_locked"] = printer_index_locked

    with config.open() as cfg:
        # If PRINTER_INDEX env var is not set, use the persisted active_printer_index
        # from the config file (only if it is within valid range).
        if not printer_index_locked and cfg and hasattr(cfg, "active_printer_index"):
            saved = cfg.active_printer_index
            if 0 <= saved < len(cfg.printers):
                printer_index = saved

        if cfg and printer_index >= len(cfg.printers):
            log.critical(f"Printer number {printer_index} out of range, max printer number is {len(cfg.printers)-1} ")
        video_supported = False
        active_model = None
        if cfg and printer_index < len(cfg.printers):
            active_model = cfg.printers[printer_index].model
            video_supported = active_model not in PRINTERS_WITHOUT_CAMERA
        unsupported = active_model in UNSUPPORTED_PRINTERS if active_model else False
        app.config["config"] = config
        app.config["login"] = bool(cfg)
        app.config["printer_index"] = printer_index
        app.config["port"] = port
        app.config["host"] = host
        app.config["insecure"] = insecure
        app.config["video_supported"] = video_supported
        app.config["unsupported_device"] = unsupported
        app.config.update(kwargs)
        has_supported_printer = any(
            printer.model not in UNSUPPORTED_PRINTERS
            for printer in getattr(cfg, "printers", [])
        ) if cfg else False
        if cfg and has_supported_printer:
            register_services(app)
        if cfg and unsupported:
            log.warning(
                f"Active device {active_model} is not supported by ankerctl — "
                "printer-control endpoints stay blocked, but supported printers keep their MQTT observers."
            )

    @app.context_processor
    def inject_debug():
        return {"debug_mode": os.getenv("ANKERCTL_DEV_MODE", "false").lower() == "true"}

    _configure_access_log_noise()
    app.run(host=host, port=port)


if os.getenv("ANKERCTL_DEV_MODE", "false").lower() == "true":
    @app.get("/api/debug/state")
    def app_api_debug_state():
        with borrow_mqtt() as mqtt:
            if not mqtt:
                return {"error": "Service unavailable"}, 503
            return mqtt.get_state()

    @app.post("/api/debug/config")
    def app_api_debug_config():
        payload = request.get_json(silent=True) or {}
        debug_logging = payload.get("debug_logging")
        with borrow_mqtt() as mqtt:
            if not mqtt:
                return {"error": "Service unavailable"}, 503
            if debug_logging is not None:
                mqtt.set_debug_logging(bool(debug_logging))
        return {"status": "ok"}

    @app.post("/api/debug/simulate")
    def app_api_debug_simulate():
        payload = request.get_json(silent=True) or {}
        event_type = payload.get("type")
        event_payload = payload.get("payload") or {}
        with borrow_mqtt() as mqtt:
            if not mqtt:
                return {"error": "Service unavailable"}, 503
            mqtt.simulate_event(event_type, event_payload)
        return {"status": "ok"}

    @app.get("/api/debug/logs")
    def app_api_debug_logs_list():
        import glob
        if not _log_dir:
            return {"files": [], "warning": "No log directory configured (set ANKERCTL_LOG_DIR)"}
        files = glob.glob(os.path.join(_log_dir, "*.log"))
        return {"files": sorted([os.path.basename(f) for f in files])}

    @app.get("/api/debug/logs/<filename>")
    def app_api_debug_logs_content(filename):
        import collections
        if not _log_dir:
            return {"error": "No log directory configured (set ANKERCTL_LOG_DIR)"}, 404
        # basic path traversal protection
        if "/" in filename or "\\" in filename or ".." in filename:
            return {"error": "Invalid filename"}, 400

        filepath = os.path.join(_log_dir, filename)
        if not os.path.realpath(filepath).startswith(os.path.realpath(_log_dir) + os.sep):
            return {"error": "Invalid filename"}, 400
        if not os.path.exists(filepath):
            return {"error": "File not found"}, 404

        lines_count = max(1, min(request.args.get("lines", 500, type=int), 10000))

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                # Use collections.deque to keep only last N lines efficiently
                lines = list(collections.deque(f, lines_count))
                content = "".join(lines)
                return {"filename": filename, "content": content}
        except Exception as e:
            return {"error": str(e)}, 500

    @app.get("/api/debug/services")
    def app_api_debug_services():
        result = {}
        for name, svc in app.svc.svcs.items():
            result[name] = {
                "state": svc.state.name,
                "wanted": svc.wanted,
                "refs": app.svc.refs.get(name, 0),
                "type": type(svc).__name__,
            }
        return {"services": result}

    @app.post("/api/debug/services/<name>/restart")
    def app_api_debug_service_restart(name):
        if name not in app.svc.svcs:
            return {"error": f"Unknown service: {name}"}, 404
        svc = app.svc.svcs[name]
        threading.Thread(target=svc.restart, daemon=True).start()
        return {"status": "restarting"}

    @app.post("/api/debug/services/<name>/test")
    def app_api_debug_service_test(name):
        """Run a quick connectivity probe for a PPPP service."""
        if name != LEGACY_PPPP_SERVICE_NAME and not name.startswith(PPPP_SERVICE_PREFIX):
            return {"error": f"Test not supported for service '{name}'"}, 400

        import web.service.pppp as pppp_svc
        config = app.config["config"]
        idx = app.config["printer_index"]
        if name.startswith(PPPP_SERVICE_PREFIX):
            try:
                idx = int(name.split(":", 1)[1])
            except (TypeError, ValueError):
                return {"error": f"Invalid PPPP service name '{name}'"}, 400

        if not config:
            return {"error": "No printer configured"}, 503

        ok = pppp_svc.probe_pppp(config, idx)
        return {"result": "ok" if ok else "fail"}

    @app.get("/api/debug/bed-leveling")
    def app_api_debug_bed_leveling():
        """Read the bed leveling grid from the printer (debug endpoint).

        Delegates to _read_bed_leveling_grid() for the actual work.
        """
        data, err = _read_bed_leveling_grid()
        if err is not None:
            return err
        return data

    @app.get("/api/debug/printer-report/<name>")
    def app_api_debug_printer_report(name):
        try:
            return _read_printer_report(name)
        except KeyError:
            return {"error": f"Unknown printer report: {name}"}, 404
        except TimeoutError as exc:
            return {"error": str(exc)}, 504
        except ConnectionError as exc:
            return {"error": str(exc)}, 503


@app.after_request
def add_security_headers(response):
    """Add security-relevant HTTP headers to every response."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Server'] = 'ankerctl'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
        "media-src 'self' blob:;"
    )
    return response


# GET endpoints that modify state or expose sensitive debug information
# and therefore require auth even though they are GET requests.
_PROTECTED_GET_PATHS = {
    "/api/ankerctl/server/reload",
    "/api/console/logs",
    "/api/debug/state",
    "/api/debug/logs",
    "/api/debug/services",
    "/api/camera/frame",
    "/api/snapshot",
    # Sensitive credential exposure: HA MQTT password, Apprise URLs/keys
    "/api/settings/mqtt",
    "/api/settings/filament-service",
    "/api/settings/timelapse",
    "/api/notifications/settings",
    # Exposes printer serial numbers, IP addresses, and MAC addresses
    "/api/printers",
    "/api/printer/bed-leveling",
    "/api/printer/bed-leveling/last",
    "/api/printer/settings-summary",
    "/api/printer/z-offset",
    # Exposes full print history (filenames, timestamps, durations)
    "/api/filaments",
    "/api/filaments/service/swap",
    "/api/history",
    "/api/timelapses",
    "/api/timelapse-snapshots",
}

# POST endpoints needed for initial printer setup (config import / login)
_SETUP_PATHS = {
    "/api/ankerctl/config/upload",
    "/api/ankerctl/config/login",
}

# URL path prefixes that send commands to the printer and must be blocked
# when the active device is not supported (e.g. eufyMake E1 UV printer).
# Any request whose path starts with one of these prefixes is rejected.
# "/api/filaments" covers both "/api/filaments" (list/create) and
# "/api/filaments/<id>/apply" (preheat), so no separate exact-match set is needed.
_PRINTER_CONTROL_PREFIXES = (
    "/api/printer/",
    "/api/files/",
    "/api/filaments",
)


@app.before_request
def _require_printer_for_control():
    """Return 503 on printer-control endpoints when no printer is configured yet."""
    if app.config["login"]:
        return None
    if request.path.startswith("/static/"):
        return None
    if any(request.path.startswith(prefix) for prefix in _PRINTER_CONTROL_PREFIXES):
        return jsonify({"error": "No printer configured. Please set up ankerctl first."}), 503
    return None


@app.before_request
def _block_unsupported_device():
    """Block printer-control endpoints when the active device is unsupported.

    This guard runs before auth so that the 503 is returned even on
    unauthenticated requests — the device must simply never be commanded.
    Static assets and config/setup paths are always allowed so the UI
    remains reachable for configuration changes.
    """
    if not app.config.get("unsupported_device"):
        return None

    path = request.path

    # Always allow static assets
    if path.startswith("/static/"):
        return None

    # Block printer-control paths
    if any(path.startswith(prefix) for prefix in _PRINTER_CONTROL_PREFIXES):
        return jsonify({"error": "Active device is not supported by ankerctl"}), 503

    return None


def _safe_same_site_redirect_target(path, params=None):
    """Build a redirect target that is guaranteed to stay on this app."""
    from urllib.parse import urlencode, urlparse

    target = path or "/"
    if params:
        query = urlencode(params, doseq=True)
        if query:
            target = f"{target}?{query}"

    # Browsers accept several malformed slash variants as external URLs.
    normalized = target.replace("\\", "")
    parsed = urlparse(normalized)
    if parsed.scheme or parsed.netloc or normalized.startswith("//"):
        return url_for("app_root")

    if not normalized.startswith("/"):
        normalized = "/" + normalized.lstrip("/")
    return normalized


@app.before_request
def _check_api_key():
    """Middleware: enforce API key on write operations (POST/PUT/DELETE).

    Read-only requests (GET) are allowed without auth so the WebUI
    stays accessible.  The API key is only required for mutations.
    Setup endpoints are exempted when no printer is configured yet.
    """
    api_key = app.config.get("api_key")
    if not api_key:
        # No key configured → allow all requests (backwards compatible)
        return None

    # Allow static assets without auth
    if request.path.startswith("/static/"):
        return None

    # Check ?apikey= URL parameter early so the browser UI can bootstrap an
    # authenticated session, then redirect to strip it from the visible URL.
    url_key = request.args.get("apikey")
    if url_key and secrets.compare_digest(url_key, api_key):
        session["authenticated"] = True
        from flask import redirect
        params = {key: values for key, values in request.args.lists() if key != "apikey"}
        clean_url = _safe_same_site_redirect_target(request.path, params)
        return redirect(clean_url)

    # Check X-Api-Key header (slicer / programmatic access)
    header_key = request.headers.get("X-Api-Key")
    if header_key and secrets.compare_digest(header_key, api_key):
        return None

    # Allow read-only (GET/HEAD/OPTIONS) unless the path is explicitly protected.
    # Also protect any path under /api/debug/ (prefix match for dynamic segments).
    is_debug_path = request.path.startswith("/api/debug/")
    is_timelapse_path = (
        request.path.startswith("/api/timelapse/")
        or request.path.startswith("/api/timelapse-snapshot/")
    )
    if request.method in ("GET", "HEAD", "OPTIONS") and request.path not in _PROTECTED_GET_PATHS and not is_debug_path and not is_timelapse_path:
        return None

    # Allow setup endpoints when no printer is configured yet
    if not app.config.get("login") and request.path in _SETUP_PATHS:
        return None

    # --- From here on, auth is required ---

    # Check session cookie (browser)
    if session.get("authenticated"):
        return None

    # Unauthorized
    return jsonify({"error": "Unauthorized. Provide API key via X-Api-Key header or ?apikey= parameter."}), 401
