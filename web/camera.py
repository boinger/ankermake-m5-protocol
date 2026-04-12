import os
import subprocess
import tempfile
from urllib.parse import quote

import cli.model


CAMERA_SOURCE_PRINTER = "printer"
CAMERA_SOURCE_EXTERNAL = "external"
DEFAULT_EXTERNAL_REFRESH_SEC = 3
PRINTERS_WITHOUT_CAMERA = {"V8110"}


class CameraCaptureError(RuntimeError):
    pass


def default_camera_settings():
    return {
        "source": CAMERA_SOURCE_PRINTER,
        "external": {
            "name": "",
            "stream_url": "",
            "snapshot_url": "",
            "refresh_sec": DEFAULT_EXTERNAL_REFRESH_SEC,
        },
    }


def printer_supports_camera(printer_or_model):
    model = getattr(printer_or_model, "model", printer_or_model)
    if not model:
        return False
    return str(model) not in PRINTERS_WITHOUT_CAMERA


def _normalize_source(value):
    source = str(value or CAMERA_SOURCE_PRINTER).strip().lower()
    if source not in {CAMERA_SOURCE_PRINTER, CAMERA_SOURCE_EXTERNAL}:
        return CAMERA_SOURCE_PRINTER
    return source


def _normalize_refresh_sec(value):
    try:
        refresh_sec = int(value)
    except (TypeError, ValueError):
        refresh_sec = DEFAULT_EXTERNAL_REFRESH_SEC
    return max(1, min(refresh_sec, 30))


def normalize_external_camera_settings(data):
    merged = cli.model.merge_dict_defaults(data, default_camera_settings()["external"])
    return {
        "name": str(merged.get("name") or "").strip(),
        "stream_url": str(merged.get("stream_url") or "").strip(),
        "snapshot_url": str(merged.get("snapshot_url") or "").strip(),
        "refresh_sec": _normalize_refresh_sec(merged.get("refresh_sec")),
    }


def _printer_from_config(cfg, printer_index):
    printers = getattr(cfg, "printers", None) or []
    if printer_index < 0 or printer_index >= len(printers):
        return None
    return printers[printer_index]


def resolve_camera_settings(cfg, printer_index=0):
    printer = _printer_from_config(cfg, printer_index)
    printer_name = getattr(printer, "name", None)
    printer_sn = getattr(printer, "sn", None)
    printer_supported = printer_supports_camera(printer)

    root = cli.model.merge_dict_defaults(getattr(cfg, "camera", None), cli.model.default_camera_config())
    per_printer = root.get("per_printer")
    if not isinstance(per_printer, dict):
        per_printer = {}

    raw_entry = per_printer.get(printer_sn, {}) if printer_sn else {}
    entry = cli.model.merge_dict_defaults(raw_entry, default_camera_settings())
    source = _normalize_source(entry.get("source"))
    external = normalize_external_camera_settings(entry.get("external"))
    external_configured = bool(external["stream_url"] or external["snapshot_url"])

    effective_source = None
    if source == CAMERA_SOURCE_PRINTER and printer_supported:
        effective_source = CAMERA_SOURCE_PRINTER
    elif source == CAMERA_SOURCE_EXTERNAL and external_configured:
        effective_source = CAMERA_SOURCE_EXTERNAL
    elif source == CAMERA_SOURCE_PRINTER and not printer_supported and external_configured:
        effective_source = CAMERA_SOURCE_EXTERNAL

    if effective_source == CAMERA_SOURCE_PRINTER:
        detail = "Using the printer camera."
    elif source == CAMERA_SOURCE_EXTERNAL and not external_configured:
        detail = "External camera is selected, but no stream or snapshot URL is configured yet."
    elif not printer_supported and not external_configured:
        detail = "This printer does not expose a built-in camera. Configure an external feed in Setup -> Camera."
    elif effective_source == CAMERA_SOURCE_EXTERNAL:
        detail = f"Using external camera preview (refreshes every {external['refresh_sec']}s)."
    else:
        detail = "No camera source is ready yet."

    return {
        "printer_index": printer_index,
        "printer_name": printer_name,
        "printer_sn": printer_sn,
        "source": source,
        "effective_source": effective_source,
        "printer_supported": printer_supported,
        "feature_available": bool(printer_supported or external_configured),
        "detail": detail,
        "external": {
            **external,
            "configured": external_configured,
        },
    }


def update_camera_settings(cfg, printer_index, payload):
    if not isinstance(payload, dict):
        payload = {}

    printer = _printer_from_config(cfg, printer_index)
    if not printer:
        raise ValueError("No active printer is configured.")

    current = resolve_camera_settings(cfg, printer_index)
    source = _normalize_source(payload.get("source", current.get("source")))
    external_payload = payload.get("external")
    merged_external = normalize_external_camera_settings(
        cli.model.merge_dict_defaults(
            external_payload if isinstance(external_payload, dict) else {},
            current.get("external"),
        )
    )

    root = cli.model.merge_dict_defaults(getattr(cfg, "camera", None), cli.model.default_camera_config())
    per_printer = root.get("per_printer")
    if not isinstance(per_printer, dict):
        per_printer = {}
    per_printer[printer.sn] = {
        "source": source,
        "external": merged_external,
    }
    root["per_printer"] = per_printer
    cfg.camera = root
    return resolve_camera_settings(cfg, printer_index)


def runtime_camera_state(camera_settings):
    external = camera_settings.get("external") or {}
    return {
        "source": camera_settings.get("source"),
        "effective_source": camera_settings.get("effective_source"),
        "printer_supported": bool(camera_settings.get("printer_supported")),
        "feature_available": bool(camera_settings.get("feature_available")),
        "detail": camera_settings.get("detail"),
        "external_name": external.get("name") or None,
        "external_configured": bool(external.get("configured")),
        "external_refresh_sec": external.get("refresh_sec") or DEFAULT_EXTERNAL_REFRESH_SEC,
    }


def create_temp_snapshot_file():
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    temp_path = temp_file.name
    temp_file.close()
    return temp_path


def build_printer_video_url(host, port, api_key=None, *, for_timelapse=False, printer_index=None):
    url = f"http://{host}:{port}/video"
    query = []
    if for_timelapse:
        query.append("for_timelapse=1")
    if printer_index is not None:
        query.append(f"printer_index={int(printer_index)}")
    if api_key:
        query.append(f"apikey={quote(api_key, safe='')}")
    if query:
        url = f"{url}?{'&'.join(query)}"
    return url


def _scale_filter(scale):
    if not scale:
        return None
    width, height = scale
    return (
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2"
    )


def _run_ffmpeg_snapshot(ffmpeg_path, input_url, output_path, *, timeout, input_args=None, format_hint=None, scale=None):
    attempts = []
    if format_hint:
        attempts.append(["-f", format_hint])
    attempts.append([])

    last_stderr = ""
    scale_filter = _scale_filter(scale)
    for hint_args in attempts:
        cmd = [ffmpeg_path, "-loglevel", "error", "-nostdin", "-y"]
        if input_args:
            cmd.extend(input_args)
        cmd.extend(hint_args)
        cmd.extend(["-i", input_url, "-frames:v", "1"])
        if scale_filter:
            cmd.extend(["-vf", scale_filter])
        cmd.append(output_path)
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            return
        last_stderr = result.stderr.decode("utf-8", errors="replace").strip()

    try:
        os.remove(output_path)
    except OSError:
        pass
    raise CameraCaptureError(last_stderr or "Snapshot capture failed")


def _external_input_url(camera_settings):
    external = (camera_settings or {}).get("external") or {}
    return external.get("snapshot_url") or external.get("stream_url") or ""


def capture_camera_snapshot_to_file(
    camera_settings,
    ffmpeg_path,
    output_path,
    *,
    host,
    port,
    api_key=None,
    timeout=30,
    for_timelapse=False,
    scale=None,
):
    effective_source = (camera_settings or {}).get("effective_source")
    if effective_source == CAMERA_SOURCE_PRINTER:
        input_url = build_printer_video_url(
            host,
            port,
            api_key,
            for_timelapse=for_timelapse,
            printer_index=(camera_settings or {}).get("printer_index"),
        )
        _run_ffmpeg_snapshot(
            ffmpeg_path,
            input_url,
            output_path,
            timeout=timeout,
            format_hint="h264",
            scale=scale,
        )
        return

    if effective_source == CAMERA_SOURCE_EXTERNAL:
        input_url = _external_input_url(camera_settings)
        if not input_url:
            raise CameraCaptureError("External camera is selected, but no stream or snapshot URL is configured.")
        input_args = []
        if input_url.lower().startswith("rtsp://"):
            input_args.extend(["-rtsp_transport", "tcp"])
        _run_ffmpeg_snapshot(
            ffmpeg_path,
            input_url,
            output_path,
            timeout=timeout,
            input_args=input_args,
            scale=scale,
        )
        return

    source = (camera_settings or {}).get("source")
    if source == CAMERA_SOURCE_EXTERNAL:
        raise CameraCaptureError("External camera is selected, but no stream or snapshot URL is configured.")
    raise CameraCaptureError("No camera source is available for this printer.")
