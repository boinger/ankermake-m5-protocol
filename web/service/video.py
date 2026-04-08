import json
import logging
import time

from queue import Empty

_STALL_TIMEOUT = 5.0  # seconds without a frame before soft restart; 3 failures → ServiceRestartSignal
_STALL_MAX_RETRIES = 3  # escalate to hard restart after this many consecutive soft-reset failures
_LIVE_REFRESH_COOLDOWN = 4.0

log = logging.getLogger(__name__)

from ..lib.service import Service, ServiceRestartSignal, ServiceStoppedError, RunState
from .. import app
from .pppp import PPPPService

from libflagship.pppp import P2PSubCmdType, P2PCmdType, Xzyh


VIDEO_PROFILES = [
    {
        "id": "sd",
        "label": "SD",
        "display": "SD (848x480)",
        "width": 848,
        "height": 480,
        "live": True,
        "live_mode": 0,
    },
    {
        "id": "hd",
        "label": "HD",
        "display": "HD (720p)",
        "width": 1280,
        "height": 720,
        "live": True,
        "live_mode": 1,
    },
    {
        "id": "fhd",
        "label": "FHD",
        "display": "FHD (1080p) - snapshot only",
        "width": 1920,
        "height": 1080,
        "live": False,
        "live_mode": None,
    },
]

VIDEO_PROFILES_BY_ID = {profile["id"]: profile for profile in VIDEO_PROFILES}
VIDEO_PROFILES_BY_MODE = {
    profile["live_mode"]: profile
    for profile in VIDEO_PROFILES
    if profile.get("live_mode") is not None
}
VIDEO_PROFILE_DEFAULT_ID = "hd" if "hd" in VIDEO_PROFILES_BY_ID else VIDEO_PROFILES[0]["id"]


class VideoQueue(Service):
    def __init__(self):
        self.video_enabled = False
        self.last_frame_at = None
        self._enable_generation = 0  # increments each time video is enabled
        self._viewer_count = 0
        self._pppp_ref_held = False
        self._recycle_pppp_on_restart = False
        self._awaiting_pppp_recycle = False
        self._pending_disable = False
        self._in_place_recovery = False
        self._pppp_recycle_requested_at = None
        super().__init__()

    def api_start_live(self):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False
        live_auth = self._live_auth_data()
        if not live_auth:
            log.warning("VideoQueue: cannot start live view because live auth data is missing")
            return False
        self.pppp.api_command(P2PSubCmdType.START_LIVE, data={
            "encryptkey": live_auth["encryptkey"],
            "accountId": live_auth["accountId"],
        })
        return True

    def _live_auth_data(self):
        """Return the live-view auth payload without logging sensitive values."""
        try:
            with app.config["config"].open() as cfg:
                if not cfg:
                    return None
                printer_index = app.config.get("printer_index", 0)
                if printer_index < 0 or printer_index >= len(cfg.printers):
                    return None
                printer = cfg.printers[printer_index]
                account = getattr(cfg, "account", None)
                encryptkey = getattr(printer, "p2p_key", None)
                account_id = getattr(account, "user_id", None)
                if not encryptkey or not account_id:
                    return None
                return {
                    "encryptkey": encryptkey,
                    "accountId": account_id,
                }
        except Exception as exc:
            log.warning(f"VideoQueue: failed to load live auth data: {exc}")
            return None

    def api_stop_live(self):
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False
        self.pppp.api_command(P2PSubCmdType.CLOSE_LIVE)
        return True

    def api_light_state(self, light):
        self.saved_light_state = light  # Track desired state regardless of connection
        if not self.pppp:
            return False
        self.pppp.api_command(P2PSubCmdType.LIGHT_STATE_SWITCH, data={
            "open": light,
        })
        log.info(f"VideoQueue: light {'on' if light else 'off'}")
        return True

    def api_video_mode(self, mode):
        try:
            mode = int(mode)
        except (TypeError, ValueError):
            log.warning(f"{self.name}: Invalid video mode {mode!r}")
            return False
        self.saved_video_mode = mode
        profile = VIDEO_PROFILES_BY_MODE.get(mode)
        if profile:
            self.saved_video_profile_id = profile["id"]
        if not self.pppp:
            return False
        self.pppp.api_command(P2PSubCmdType.LIVE_MODE_SET, data={"mode": mode})
        return True

    def api_video_profile(self, profile_id):
        if profile_id is None:
            return False
        profile_key = str(profile_id).lower()
        profile = VIDEO_PROFILES_BY_ID.get(profile_key)
        if not profile:
            log.warning(f"{self.name}: Unknown video profile {profile_id!r}")
            return False
        self.saved_video_profile_id = profile["id"]
        live_mode = profile.get("live_mode")
        if not profile.get("live") or live_mode is None:
            log.info(f"{self.name}: Video profile {profile_id!r} is not supported for live view")
            return False
        self.saved_video_mode = live_mode
        if not self.pppp:
            return True
        self.pppp.api_command(P2PSubCmdType.LIVE_MODE_SET, data={"mode": live_mode})
        return True

    def _handler(self, data):
        chan, msg = data

        if chan != 1:
            return

        if not isinstance(msg, Xzyh):
            return

        if msg.cmd != P2PCmdType.APP_CMD_VIDEO_FRAME:
            log.debug(f"VideoQueue: ignoring non-video XZYH command {msg.cmd!r}")
            return

        self.last_frame_at = time.monotonic()
        self._live_active = True
        self._stall_retry_count = 0
        self.notify(msg)

    def _start_live_if_needed(self, force=False):
        if not self.video_enabled or not self.wanted:
            return False
        if not self.pppp or not getattr(self.pppp, "connected", False):
            return False

        now = time.monotonic()
        if not force and self._live_active and (now - self._last_start_live_at) < 10.0:
            log.info("VideoQueue: START_LIVE suppressed (already active recently)")
            return False

        self._last_start_live_at = now
        self._live_started_at = now
        self.last_frame_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0

        log.info("VideoQueue: calling api_start_live()")
        if not self.api_start_live():
            return False

        # Only mark live after the first frame arrives.
        self._live_active = False
        return True

    def _ensure_pppp_ready(self):
        """Return a ready PPPP service while preserving the existing borrow across
        internal VideoQueue restarts.

        Reacquiring PPPP on every VideoQueue restart causes a ref drop to zero,
        which stops PPPP and then races with the next video start. Keep the PPPP
        borrow alive across ordinary internal video restarts and only release it
        when video is actually being disabled, the app is shutting down, or a
        hard recovery explicitly requests a full PPPP recycle.
        """
        if self._pppp_ref_held and self.pppp is not None:
            try:
                self.pppp.await_ready()
            except ServiceStoppedError:
                try:
                    app.svc.put("pppp")
                except Exception:
                    pass
                self._pppp_ref_held = False
                self.pppp = None
            else:
                return self.pppp

        pppp_svc = getattr(app.svc, "svcs", {}).get("pppp")
        if self._awaiting_pppp_recycle and pppp_svc is not None:
            effectively_stopped = (
                not getattr(pppp_svc, "wanted", False)
                and not getattr(pppp_svc, "connected", False)
                and not hasattr(pppp_svc, "_api")
            )

            if pppp_svc.state != RunState.Stopped:
                recycle_requested_at = getattr(self, "_pppp_recycle_requested_at", None)
                stuck_after_forced_recycle = (
                    not getattr(pppp_svc, "wanted", False)
                    and recycle_requested_at is not None
                    and (time.monotonic() - recycle_requested_at) >= 2.0
                )
                if not effectively_stopped and not stuck_after_forced_recycle:
                    return None

                if stuck_after_forced_recycle and not effectively_stopped:
                    log.warning("VideoQueue: PPPP stop is stuck after forced recycle; marking service reusable")

                log.info("VideoQueue: PPPP recycle is effectively complete; proceeding with fresh reacquire")
                # The old PPPP worker can get wedged after a video freeze.
                # Replace the managed PPPP service with a fresh instance so a
                # new connection attempt is guaranteed to start from a clean
                # thread/service object instead of reusing stale state.
                app.svc.replace_service("pppp", PPPPService())
                pppp_svc = app.svc.svcs.get("pppp")

            self._awaiting_pppp_recycle = False
            self._pppp_recycle_requested_at = None
            log.info("VideoQueue: PPPP recycle completed; reacquiring fresh PPPP session")

        # Important: during a recycle handoff, do not block waiting for PPPP to
        # become ready. Borrow it in non-ready mode and let worker_start/
        # worker_run observe the connection state naturally.
        self.pppp = app.svc.get("pppp", ready=False)
        self._pppp_ref_held = True
        return self.pppp

    def worker_init(self):
        self.saved_light_state = None
        self.saved_video_mode = None
        self.saved_video_profile_id = None
        self.last_frame_at = None
        self._live_started_at = None
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0
        self._last_start_live_at = 0.0
        self._live_active = False
        self.api_id = None
        self.pppp = None
        self._pppp_ref_held = False
        self._stall_retry_count = 0
        self._recycle_pppp_on_restart = False
        self._awaiting_pppp_recycle = False
        self._pending_disable = False
        self._in_place_recovery = False
        self._pppp_recycle_requested_at = None

    def _recycle_pppp_in_place(self):
        """Recycle the PPPP session without restarting the VideoQueue service.

        Keeping /ws/video open avoids forcing the browser to rebuild the
        websocket/JMuxer pipeline on every hard video recovery.
        """
        if not self.video_enabled or not self.wanted:
            log.info("VideoQueue: PPPP recycle skipped because video was disabled")
            return

        self._in_place_recovery = True
        try:
            try:
                self.api_stop_live()
            except Exception:
                pass

            old_pppp = self.pppp
            if old_pppp is not None:
                try:
                    with old_pppp._handler_lock:
                        if self._handler in old_pppp.xzyh_handlers:
                            old_pppp.xzyh_handlers.remove(self._handler)
                except Exception as exc:
                    log.debug(f"VideoQueue: failed detaching handler during in-place recovery: {exc}")

            if self._pppp_ref_held:
                log.info("VideoQueue: recycling PPPP in place for video recovery")
                self._awaiting_pppp_recycle = True
                self._pppp_recycle_requested_at = time.monotonic()
                app.svc.put("pppp")
                self._pppp_ref_held = False

            self.pppp = None
            self.api_id = None
            self._live_active = False
            self._last_start_live_at = 0.0
            self._live_started_at = None
            self.last_frame_at = None
            self._last_no_frame_log_at = 0.0
            self._last_live_refresh_at = 0.0
            self._stall_retry_count = 0
            log.info("VideoQueue: PPPP recycle requested in place; waiting for clean stop before reacquire")
        finally:
            self._in_place_recovery = False

    def worker_start(self):
        self._stall_retry_count = 0
        if not self.video_enabled:
            return

        self.pppp = self._ensure_pppp_ready()
        if not self.pppp:
            log.debug("VideoQueue: PPPP not available yet in worker_start")
            self.api_id = None
            return
        if not getattr(self.pppp, "connected", False):
            log.debug("VideoQueue: PPPP exists but is not connected yet in worker_start")
            self.api_id = None
            return
        if not hasattr(self.pppp, "_api"):
            log.debug("VideoQueue: PPPP connected but API not ready yet in worker_start")
            self.api_id = None
            return

        self.api_id = id(self.pppp._api)

        with self.pppp._handler_lock:
            if self._handler not in self.pppp.xzyh_handlers:
                self.pppp.xzyh_handlers.append(self._handler)
        self._start_live_if_needed(force=False)

        if self.saved_light_state is not None:
            self.api_light_state(self.saved_light_state)

        if self.saved_video_profile_id is not None:
            applied = self.api_video_profile(self.saved_video_profile_id)
            if not applied and self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)
        elif self.saved_video_mode is not None:
            self.api_video_mode(self.saved_video_mode)

    def worker_run(self, timeout):
        if not self.video_enabled:
            return
        self.idle(timeout=timeout)
        if not self.video_enabled or not self.wanted:
            return

        if self._in_place_recovery:
            time.sleep(0.1)
            return

        if not self.pppp:
            self.pppp = self._ensure_pppp_ready()
            if not self.pppp:
                log.debug("VideoQueue: PPPP not available yet")
                time.sleep(0.5)
                return

        # Snapshot the PPPP reference to avoid TOCTOU races if the service
        # restarts between checks.  All subsequent accesses use this local.
        pppp = self.pppp
        if pppp is None:
            raise ServiceRestartSignal("PPPP reference lost during video session")

        if not getattr(pppp, "connected", False):
            log.debug("VideoQueue: PPPP exists but is not connected yet")
            time.sleep(0.5)
            return

        if getattr(self, "api_id", None) is None:
            if not hasattr(pppp, "_api"):
                log.debug("VideoQueue: PPPP connected but API not ready yet")
                time.sleep(0.5)
                return

            self.api_id = id(pppp._api)
            with pppp._handler_lock:
                if self._handler not in pppp.xzyh_handlers:
                    pppp.xzyh_handlers.append(self._handler)

            started = self._start_live_if_needed(force=False)
            if not started:
                log.info("VideoQueue: Failed to start live view during late init")
                time.sleep(0.5)
                return

            if self.saved_light_state is not None:
                self.api_light_state(self.saved_light_state)

            if self.saved_video_profile_id is not None:
                applied = self.api_video_profile(self.saved_video_profile_id)
                if not applied and self.saved_video_mode is not None:
                    self.api_video_mode(self.saved_video_mode)
            elif self.saved_video_mode is not None:
                self.api_video_mode(self.saved_video_mode)

            log.info("VideoQueue: Live video started after PPPP became ready")
            return

        if not hasattr(pppp, "_api"):
            raise ServiceRestartSignal("PPPP lost during video session")

        if id(pppp._api) != self.api_id:
            raise ServiceRestartSignal("New pppp connection detected, restarting video feed")

        if self.handlers:
            now = time.monotonic()
            if self.last_frame_at is not None:
                gap = now - self.last_frame_at
                if gap > _STALL_TIMEOUT:
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._attempt_stall_recovery(
                            pppp,
                            f"VideoQueue: No video frames for {gap:.1f}s; restarting live stream",
                            "VideoQueue: Failed to restart live stream",
                            "Video stall recovery exhausted",
                        )
                    time.sleep(0.5)
                    return
            elif self._live_started_at is not None:
                since_start = now - self._live_started_at
                if since_start > _STALL_TIMEOUT:
                    if now - self._last_no_frame_log_at >= 10.0:
                        log.info(f"VideoQueue: No initial frame yet after {since_start:.1f}s; waiting")
                        self._last_no_frame_log_at = now
                    if now - self._last_live_refresh_at >= _LIVE_REFRESH_COOLDOWN:
                        self._attempt_stall_recovery(
                            pppp,
                            "VideoQueue: Re-requesting live stream (no initial frame)",
                            "VideoQueue: Failed to restart live stream for initial-frame recovery",
                            "Video stall recovery exhausted (no initial frame)",
                        )
                    time.sleep(0.5)
                    return

    def _attempt_stall_recovery(self, pppp, warn_msg, retry_fail_msg, exhaust_msg):
        """Stop and restart the live stream after a stall.

        Each invocation counts as one consecutive no-frame recovery attempt.
        The counter is only reset when an actual frame arrives in _handler().
        After enough consecutive no-frame recoveries, escalate to a full
        VideoQueue/PPPP restart instead of looping forever on START_LIVE.
        """
        self._last_live_refresh_at = time.monotonic()
        self._stall_retry_count += 1
        attempt = self._stall_retry_count
        log.warning(f"{warn_msg} (attempt {attempt}/{_STALL_MAX_RETRIES})")
        try:
            self._live_active = False
            self.api_stop_live()
            time.sleep(0.5)
            if not self.video_enabled or not self.wanted:
                log.info("VideoQueue: live refresh cancelled because video was disabled")
                return
            if not pppp or not getattr(pppp, "connected", False):
                log.warning("VideoQueue: PPPP unavailable during live refresh")
                if attempt >= _STALL_MAX_RETRIES:
                    raise ServiceRestartSignal(exhaust_msg)
                time.sleep(0.5)
                return
            if not self._start_live_if_needed(force=True):
                log.warning(f"{retry_fail_msg} (attempt {attempt}/{_STALL_MAX_RETRIES})")
            if attempt >= _STALL_MAX_RETRIES:
                log.warning(f"{exhaust_msg}; recycling PPPP in place")
                self._recycle_pppp_in_place()
        except ServiceRestartSignal:
            raise
        except Exception as exc:
            log.warning(f"VideoQueue: Failed to refresh live stream ({exc})")

    def worker_stop(self):
        try:
            self.api_stop_live()
        except Exception as E:
            log.warning(f"{self.name}: Failed to send stop command ({E})")

        if self.pppp:
            with self.pppp._handler_lock:
                if self._handler in self.pppp.xzyh_handlers:
                    self.pppp.xzyh_handlers.remove(self._handler)

        release_pppp = (not self.wanted) or (not self.video_enabled) or getattr(app.svc, "shutting_down", False) or self._recycle_pppp_on_restart
        if self.pppp and self._pppp_ref_held and release_pppp:
            if self._recycle_pppp_on_restart:
                log.info("VideoQueue: releasing PPPP so it can be recycled for video recovery")
                self._awaiting_pppp_recycle = True
            app.svc.put("pppp")
            self._pppp_ref_held = False
            self.pppp = None
        elif self.pppp and self._pppp_ref_held:
            log.info("VideoQueue: keeping PPPP borrowed across video worker restart")

        self._live_active = False
        self._last_start_live_at = 0.0
        self.api_id = None
        self._live_started_at = None
        if self._recycle_pppp_on_restart and self.wanted and self.video_enabled:
            log.info("VideoQueue: PPPP will be reacquired on the next worker start")
        self._recycle_pppp_on_restart = False


    def viewer_connected(self):
        self._viewer_count += 1
        return self._viewer_count

    def viewer_disconnected(self):
        if self._viewer_count > 0:
            self._viewer_count -= 1
        if self._viewer_count == 0 and self._pending_disable:
            log.info("VideoQueue: last viewer disconnected; clearing stale deferred disable")
            self._pending_disable = False
        elif self._viewer_count == 0 and not self.video_enabled and self.state == RunState.Running:
            log.info("VideoQueue: last viewer disconnected; stopping disabled video service")
            self.stop()
        return self._viewer_count

    def set_video_enabled(self, enabled):
        if enabled:
            self._pending_disable = False
            self.persistent = True
            if self.video_enabled:
                return True
            self.video_enabled = True
            self._enable_generation += 1
            if self.state == RunState.Stopped:
                self.start()
            return True

        self.persistent = False
        if not self.video_enabled and not self._pending_disable:
            return True

        self._pending_disable = False
        self.video_enabled = False
        self._live_active = False
        self._live_started_at = None
        self.last_frame_at = None
        self._last_start_live_at = 0.0
        self._last_no_frame_log_at = 0.0
        self._last_live_refresh_at = 0.0
        self._stall_retry_count = 0
        self._awaiting_pppp_recycle = False
        self._pppp_recycle_requested_at = None
        if self._viewer_count > 0:
            log.info(f"VideoQueue: disabling video; stopping service with {self._viewer_count} video client(s) connected")
        if self.state in (RunState.Starting, RunState.Running):
            self.stop()
        elif self.state in (RunState.Stopping, RunState.Stopped):
            self.wanted = False
        return True
