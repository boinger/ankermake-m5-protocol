# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-04-13

### Added
 - External camera feed support (RTSP, HTTP, MJPEG) configurable in Setup tab
 - Snapshot gallery with timelapse integration — manual snapshots archived per print
 - Guided automatic filament swap flow: homes, raises Z, parks, heats, unloads, prompts, loads, purges, cools
 - Per-printer timelapse settings
 - Timelapse pause / resume / stop controls for running captures
 - Snapshot collection browser with individual file download and delete
 - Local G-Code archiving with reprint support from History
 - G-Code thumbnails in History, thumb drive, and printer storage lists
 - Selective delete of individual History entries
 - Filament state indicator on Home page
 - Print-complete alerts via notification path
 - Setup page with Windows launcher `.bat` download
 - Slicer login cache auto-import (OrcaSlicer / EufyMake Studio / PrusaSlicer)
 - Collapsible Home page console viewer
 - Camera frame API endpoint for live JPEG capture (`/api/camera/frame`)
 - Print-state lock on G-Code page (file lists do not refresh while printing)
 - Apprise notification integration with full web UI configuration
 - Email/password login flow as alternative to `login.json` import with CAPTCHA support
 - Home Assistant MQTT Discovery integration
 - Print history with SQLite backend, reprint support, and thumbnail previews
 - Bed level map heatmap with before/after comparison in Setup tab

### Security
 - SSRF closed: External camera URLs validated against an allowlist (`http`, `https`, `rtsp`, `rtmp`) before being passed to ffmpeg
 - Auth gap closed: `GET /api/settings/camera` now requires authentication
 - Injection fix: Newlines and null bytes rejected in Windows launcher `install_dir`
 - XSS fix: Auto-leveling progress value escaped before DOM insertion (CodeQL #21)
 - Stack trace exposure fix: Exception object no longer flows into login error response (CodeQL #25)
 - ReDoS fix: HTML tag stripping regex quantifier bounded to prevent polynomial backtracking (CodeQL #24)
 - `GET /api/settings/mqtt` and `GET /api/notifications/settings` now require auth
 - `/ws/ctrl` WebSocket enforces API key auth inline
 - Timelapse endpoints have path traversal protection

### Fixed
 - Race condition: `_viewer_count` in VideoQueue protected by threading lock
 - Resource leak: Partial MP4 deleted on ffmpeg assembly failure
 - Thread safety: Capture thread join timeout increased to exceed ffmpeg snapshot timeout
 - Ghost temperature flicker: MQTT parser correctly preserves `0°C` cooldown targets
 - PPPP status correctly shows yellow when using stale fallback IP
 - History not saving when prints start in close succession
 - PPPP live video stalls and recovery log noise
 - Stop button freeze and active-print cancel reliability
 - Homing buttons now match official app MQTT payloads
 - MQTT disconnected false-positive on second printer
 - Timelapse delete now removes matching snapshot collection
 - `multiprocessing.Queue` replaced with `queue.Queue` in service framework
 - HomeAssistantService heartbeat thread leak on MQTT reconnect
 - `/ws/ctrl` handler now catches all connection and parse errors gracefully

## [1.0.1] - 2026-04-14

### Fixed
 - Dead message overwrite in filament swap start (legacy path message was silently discarded)
 - Duplicate state update call in filament swap unload phase (first call was never visible)
 - Degree symbol inconsistency in filament swap status messages (`C` → `°C`)
 - ffmpeg stderr no longer leaks embedded URL credentials in camera capture error responses

## [Unreleased]

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
