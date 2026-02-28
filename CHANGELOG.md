# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
 - **`GET /api/settings/mqtt` now requires auth** — previously returned the HA MQTT broker password unauthenticated; added to `_PROTECTED_GET_PATHS`
 - **`GET /api/notifications/settings` now requires auth** — previously returned Apprise server URLs and API keys unauthenticated; added to `_PROTECTED_GET_PATHS`
 - **`/ws/ctrl` WebSocket now enforces API key auth inline** — `before_request` middleware does not run for WebSocket routes, so write-capable control operations (light, video quality, video enable) were previously unauthenticated when `ANKERCTL_API_KEY` was set
 - **Timelapse endpoints now have path traversal protection** — `GET /api/timelapse/<filename>` and `DELETE /api/timelapse/<filename>` now reject filenames containing `/`, `\`, or `..`, and additionally use `os.path.realpath()` to confirm the resolved path remains inside the captures directory
 - **XSS fixes in frontend** — `statusBadge()` unknown-status fallback now uses `escapeHtml()`; country code dropdown options now use `escapeHtml()` on values; GCode log now uses `document.createTextNode()` instead of string `.append()` to prevent HTML injection
 - Timelapse video URLs now use `encodeURIComponent()` instead of `escapeHtml()` (correct encoding for URL path segments)
 - `sockets` variable in `ankersrv.js` promoted from implicit global to explicit `const`

### Fixed
 - `multiprocessing.Queue` replaced with `queue.Queue` in `web/lib/service.py` — the multiprocessing variant is incompatible with the threading context used by `ServiceManager`, causing stream consumers to silently hang
 - **HomeAssistantService availability thread leak** — `_on_connect` now stops the existing heartbeat thread before spawning a new one on MQTT reconnects; previously each reconnect leaked a thread
 - **`MqttQueue.worker_stop()` now calls `self._ha.stop()`** — Home Assistant service was not shut down when the MQTT service stopped, leaving the HA broker connection and heartbeat thread running
 - `/ws/ctrl` handler now catches `ConnectionClosed`, `json.JSONDecodeError`, and `TypeError` (from `None` returned by `sock.receive()`) — previously any of these would crash the handler thread
 - Video quality `/ws/ctrl` handler now correctly accepts `str` values (`"sd"`, `"hd"`) — was checking `isinstance(..., int)` but the frontend sends strings
 - `JSON.parse()` in MQTT and PPPP WebSocket `onmessage` handlers wrapped in `try/catch` — a malformed message previously caused the handler to stop processing all subsequent messages
 - Temperature inputs: `max` attribute is now set dynamically from printer model (M5: 260°C nozzle, M5C: 300°C nozzle, both 100°C bed) via Jinja2 template; JS clamp reads the `max` attribute instead of using a hardcoded value
 - Temperature reset now uses `.val(0)` instead of `.attr("value", "0°C")` — the old form left a stale display value
 - ct=1044 MQTT handler added to frontend: populates `#print-name` with the basename of `data.filePath` when a print starts
 - Removed double `escapeHtml()` on `.textContent` assignments — `.textContent` is already safe, double-escaping caused `&amp;` to appear literally in the UI
 - `.prop("required", true)` fix for CAPTCHA input — was `.prop("required")` (reads property instead of setting it), making the field not required in the browser
 - `macro.html`: fixed `aria-controls="{{target}}"` (was hardcoded `"home"`); updated tooltip attributes to Bootstrap 5 (`data-bs-toggle`, `data-bs-placement`)
 - Video resolution label now registers a `resize` event listener so the label updates correctly when the stream resolution changes mid-session
 - Removed stale `//# sourceMappingURL=chart.umd.js.map` reference from `chart.umd.min.js` that caused a 404 console error
 - Removed test GCode files (`test_cube_50mm.gcode`, `test_cube_5mm.gcode`) from the repository
 - Updated `.gitignore`: added `.env.*`, `.claude/`, `.venv/`, `*.log`

### Added
 - Apprise notification integration with full web UI configuration
   - Event hooks for print start/finish/fail/progress and file uploads
   - Snapshot attachments for notifications (with fallback to preview image)
   - Configurable progress interval and event toggles
   - Support for environment variable overrides (`APPRISE_*`)
 - Email/password login flow as alternative to `login.json` import
   - CAPTCHA handling via browser when required
   - Support for different country regions
 - Native print stop command via MQTT (`ZZ_MQTT_CMD_PRINT_CONTROL`)
 - Print control visibility toggle in web UI (configurable via `PRINT_CONTROLS_VISIBLE`)
 - Test script for print control commands (`examples/test_print_control.py`)
 - Comprehensive CLAUDE.md guide for AI assistants and developers

### Fixed (prior)
 - **PPPP single-reader architecture** - Critical fix for "two readers one socket" bug that caused packet loss and unstable video/command connections
 - Thread-safety in PPPP AABB reply reader with locking to prevent `EOFError` crashes
 - UDP socket timeout leak in PPPP recv/send operations
 - Race conditions in `Service.tap()` handler removal
 - WebSocket URL fixes for `/ws/video` and `/ws/ctrl` (missing `//`)
 - Camera light control null check to prevent errors before PPPP connection established
 - Video frame forwarding from XZYH to VideoQueue
 - File transfer EOF/OSError handling (now properly reported as ConnectionError)
 - Stop button reliability with improved multi-GCode command handling
 - PPPP service worker start logic to prevent "No pppp connection" errors during uploads

### Performance
 - Skip expensive notification operations when events are disabled
 - Optimized progress notification emission to respect interval settings

## [1.0.1] - 2024-01-15

 - Fixes MQTT connection errors post AnkerMake Firmware Upgrades

## [1.0.0] - 2023-05-24

 - Version 1.0.0!
 - Add video streaming support to web ui
 - Add support for uploading `login.json` through web ui
 - Add print monitoring through web ui
 - Add new mqtt types to libflagship
 - Add status icons for mqtt, pppp and ctrl websocket
 - Add support for restarting web services through web ui
 - Add support for turning on/off camera light from web ui
 - Add support for controlling video mode (sd/hd) from web ui
 - Add `--pppp-dump` option for making a debug packet capture
 - Stabilized video streaming, by fixing some rare corner cases.
 - Make video stream automatically reconnect on connection loss
 - Make video stream automatically suspend when no clients are connected

## [0.9.0] - 2023-04-17

 - First version with github actions for building docker image. (thanks to @cisien)
 - Add python version checking code, to prevent confusing errors if python version is too old.

## [0.8.0] - 2023-04-06

 - First version with built-in webserver! (thanks to @lazemss for the idea and proof-of-concept)
 - Webserver implements a few OctoPrint endpoints, allowing printing directly from PrusaSlicer.
 - Added static web contents, including step-by-step guide for setting up PrusaSlicer.

## [0.7.0] - 2023-04-04

 - First version with camera streaming support!
 - Fixed many bugs in the file upload code, including ability to send files larger than 512K.
 - Fixed file transfers on Windows platforms.

## [0.6.0] - 2023-04-03

 - First version that can send print jobs to the printer over pppp!
 - Completely reworked pppp api implementation.
 - Added support for upgrading config files automatically, when possible.
 - Major code refactoring and improvements.

## [0.5.0] - 2023-03-26

 - Officially licensed as GPLv3.
 - Improved documentation.
 - Much improved documentation (thanks to @austinrdennis).
 - Added `mqtt gcode` command, making it possible to send custom gcode to the printer!
 - Added `mqtt rename-printer` command.
 - Added `pppp lan-search` command.
 - Added `http calc-check-code` command.
 - Added `http calc-sec-code` command.

## [0.4.0] - 2023-03-22

 - First version with the command line tool: `ankerctl.py`.
 - Added `mqtt monitor` command.
 - Added `config import` command.
 - Added `config show` command.
 - Many fixes and improvements from @spuder.

## [0.3.0] - 2023-03-12

 - Examples moved to `examples/`.
 - Added example program that imports `login.json` from Ankermake Slicer.

## [0.3.0] - 2023-03-09

 - First version with a demo program, showing how to parse pppp packets.

## [0.1.0] - 2023-03-07

 - Early code for libflagship, and first version with a README.
