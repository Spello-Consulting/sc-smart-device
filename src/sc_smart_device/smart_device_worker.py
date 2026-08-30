"""SmartDeviceWorker — threaded worker for SCSmartDevice.

Processes `DeviceSequenceRequest` jobs from a queue,
performing output changes, status refreshes, location lookups and sleeps with
per-step retry/back-off support.

Typical usage::

    import threading
    from sc_smart_device import SCSmartDevice, SmartDeviceWorker, DeviceSequenceRequest, DeviceStep, StepKind

    wake_event = threading.Event()
    sd = SCSmartDevice(logger, device_settings, wake_event)
    worker = SmartDeviceWorker(sd, logger, wake_event)

    # To reload config at runtime:
    # worker.reinitialise_settings(new_device_settings)

    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()

    req = DeviceSequenceRequest(steps=[DeviceStep(StepKind.REFRESH_STATUS)])
    req_id = worker.submit(req)
    worker.wait_for_result(req_id, timeout=10)
    view = worker.get_latest_status()
"""

from __future__ import annotations

import copy
import queue
import threading
import time
from typing import TYPE_CHECKING

from sc_smart_device.models.smart_device_view import SmartDeviceView
from sc_smart_device.models.worker_types import (
    DeviceSequenceRequest,
    DeviceSequenceResult,
    DeviceStep,
    StepKind,
)

if TYPE_CHECKING:
    from sc_foundation import SCLogger

    from sc_smart_device.smart_device import SCSmartDevice


class SmartDeviceWorker:
    """Runs a background loop that processes `DeviceSequenceRequest` jobs.

    The worker is designed to run in its own thread (pass [run][sc_smart_device.SmartDeviceWorker.run] as the thread target).
    All public methods are thread-safe.

    Args:
        smart_device: A fully-initialised [SCSmartDevice][sc_smart_device.SCSmartDevice]
            instance.  The worker does not take ownership — the caller is
            responsible for calling [shutdown][sc_smart_device.SCSmartDevice.shutdown]
            when done.
        logger: SCLogger instance from sc-foundation.
        wake_event: `threading.Event` that is ``set()`` whenever the worker
            finishes a request, so that the main application thread can react.
        max_concurrent_errors: Number of consecutive sequence errors before an
            additional notifiable issue is raised (default 4).
        critical_error_report_delay_mins: Minutes to delay before sending a
            critical-error notification.  ``None`` disables notifications.
    """

    def __init__(
        self,
        smart_device: SCSmartDevice,
        logger: SCLogger,
        wake_event: threading.Event,
        max_concurrent_errors: int = 4,
        critical_error_report_delay_mins: int | None = None,
    ) -> None:
        self._smart_device = smart_device
        self.logger = logger
        self.wake_event = wake_event
        self.stop_event = threading.Event()

        # Error reporting
        self.concurrent_error_count: int = 0
        self.max_device_errors: int = max_concurrent_errors
        self.report_critical_errors_delay: int | None = (
            round(critical_error_report_delay_mins)
            if isinstance(critical_error_report_delay_mins, (int, float))
            else None
        )

        # Command queue
        self._req_q: queue.Queue[DeviceSequenceRequest] = queue.Queue()
        self._results: dict[str, DeviceSequenceResult] = {}
        self._results_lock = threading.Lock()
        self._result_events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

        # Latest status snapshot (thread-safe)
        self._lookup_lock = threading.Lock()
        self._latest_status: SmartDeviceView = SmartDeviceView.__new__(SmartDeviceView)  # placeholder
        self._location_data: dict[str, dict] = {}

        # Track whether all devices were online last time we checked
        self.all_devices_online = True
        for device in self._smart_device.devices:
            if not device.get("Online", False):
                self.all_devices_online = False

        # Immediately capture the initial snapshot
        self._save_latest_status()

    # ── Public API (thread-safe) ─────────────────────────────────────────────

    def reinitialise_settings(self, device_settings: dict | None = None) -> None:
        """Re-apply device settings and refresh status.

        Args:
            device_settings: Updated ``SCSmartDevices`` config dict to pass to
                [initialize_settings][sc_smart_device.SCSmartDevice.initialize_settings].  Pass
                ``None`` (the default) to skip re-loading config and only trigger
                a full status refresh.
        """
        with self._results_lock:
            if device_settings is not None:
                self._smart_device.initialize_settings(device_settings, refresh_status=False)
            self.concurrent_error_count = 0
        self._refresh_all_status()

    def submit(self, req: DeviceSequenceRequest) -> str:
        """Enqueue a `DeviceSequenceRequest` for execution.

        Args:
            req: The sequence request to enqueue.

        Returns:
            The request ``id`` string; pass to [get_result][sc_smart_device.SmartDeviceWorker.get_result] or
            [wait_for_result][sc_smart_device.SmartDeviceWorker.wait_for_result].
        """
        ev = threading.Event()
        with self._events_lock:
            self._result_events[req.id] = ev
        self._req_q.put(req)
        return req.id

    def get_result(self, req_id: str) -> DeviceSequenceResult | None:
        """Return the result for a completed request, or ``None`` if not yet done.

        Args:
            req_id: The request ID returned by [submit][sc_smart_device.SmartDeviceWorker.submit].

        Returns:
            `DeviceSequenceResult` or ``None``.
        """
        with self._results_lock:
            return self._results.get(req_id)

    def wait_for_result(self, req_id: str, timeout: float | None = None) -> bool:
        """Block until a specific request completes or until *timeout* seconds elapse.

        Args:
            req_id: The request ID returned by [submit][sc_smart_device.SmartDeviceWorker.submit].
            timeout: Maximum seconds to wait.  ``None`` waits indefinitely.

        Returns:
            ``True`` if the request completed before the timeout.
        """
        with self._events_lock:
            ev = self._result_events.get(req_id)
        if ev is None:
            # Unknown id — either already collected or never submitted
            return True
        return ev.wait(timeout=timeout)

    def get_latest_status(self) -> SmartDeviceView:
        """Return a frozen snapshot of the most recently refreshed device state.

        Thread-safe; safe to call from any thread.

        Returns:
            [SmartDeviceView][sc_smart_device.SmartDeviceView] snapshot.
        """
        with self._lookup_lock:
            return self._latest_status

    def get_location_info(self) -> dict[str, dict]:
        """Return the latest device location data, keyed by device name.

        Returns:
            Dict mapping device name → location dict (``tz``, ``lat``, ``lon``).
        """
        with self._lookup_lock:
            return self._location_data

    def request_refresh_status(self) -> str:
        """Enqueue a status-refresh job and return its request id.

        Convenience wrapper around [submit][sc_smart_device.SmartDeviceWorker.submit] for the common case of
        triggering a full status refresh.

        Returns:
            Request ID string.
        """
        req = DeviceSequenceRequest(
            steps=[DeviceStep(StepKind.REFRESH_STATUS)],
            label="refresh_status",
        )
        return self.submit(req)

    def request_device_location(self, device_name: str) -> str:
        """Enqueue a location-lookup job for a single device.

        Args:
            device_name: The name (or ID) of the device to locate.

        Returns:
            Request ID string.
        """
        steps = [
            DeviceStep(
                StepKind.GET_LOCATION,
                {"device_identity": device_name},
                retries=1,
                retry_backoff_s=1.0,
            ),
        ]
        req = DeviceSequenceRequest(
            steps=steps,
            label=f"get_location_for_{device_name}",
            timeout_s=10.0,
        )
        return self.submit(req)

    # ── SCSmartDevice pass-throughs ──────────────────────────────────────────

    def is_device_online(self, device_identity: dict | int | str | None = None) -> bool:
        """See if a device is alive by pinging it.

        Returns the result and updates the device's online status. If we are in simulation mode, always returns True.

        Args:
            device_identity: The actual device object, device component object, device ID or device name of the device to check. If None, checks all devices.

        Returns:
            True if the device is online, False otherwise. If all devices are checked, returns True if all devices are online.

        Raises:
            RuntimeError: If the device is not found in the list of devices.
        """  # noqa: DOC502
        return self._smart_device.is_device_online(device_identity)

    def pull_webhook_event(
        self,
        device_id: int | None = None,
        device_name: str | None = None,
        component_id: int | None = None,
        component_name: str | None = None,
    ) -> dict | None:
        """Return the oldest queued webhook event matching the filters, and remove it.

        With no filters, returns the oldest queued event. When one or more
        filters are supplied, returns the oldest event for which *every*
        supplied filter matches (unsupplied filters are ignored).

        Args:
            device_id: Only match events whose ``Device.ID`` equals this.
            device_name: Only match events whose ``Device.Name`` equals this.
            component_id: Only match events whose ``Component.ID`` equals this.
            component_name: Only match events whose ``Component.Name`` equals this.

        Returns:
            Event dict with keys ``timestamp``, ``Event``, ``Device``,
            ``Component``, etc.; or None if no queued event matches.
        """
        return self._smart_device.pull_webhook_event(device_id, device_name, component_id, component_name)

    def does_device_have_webhooks(self, device: dict) -> bool:
        """Return True if any component of the device has webhooks enabled."""
        return self._smart_device.does_device_have_webhooks(device)

    def get_device_information(
        self, device_identity: dict | int | str, refresh_status: bool = False
    ) -> dict:
        """Return a consolidated copy of one device and all its components.

        Args:
            device_identity: Device dict, ID, or name.
            refresh_status: If True, fetch fresh state from hardware first.

        Returns:
            Device dict augmented with ``Inputs``, ``Outputs``, ``Meters``,
            and ``TempProbes`` sub-lists.

        Raises:
            RuntimeError: If the device is not found in the list of devices or if there is an error getting the status.
        """  # noqa: DOC502
        return self._smart_device.get_device_information(device_identity, refresh_status)

    def print_device_status(self, device_identity: int | str | None = None) -> str:
        """Print the status of a device or all devices.

        Args:
            device_identity: The ID or name of the device to check. If None, checks all devices.

        Returns:
            A string representation of the device status.

        Raises:
            RuntimeError: If the device is not found in the list of devices.
        """  # noqa: DOC502
        return self._smart_device.print_device_status(device_identity)

    def print_model_library(self, mode_str: str = "brief", model_id: str | None = None) -> str:
        """Print the model library for all providers that have one.

        Args:
            mode_str: The mode of printing. Can be ``"brief"`` or ``"detailed"``. Defaults to ``"brief"``.
            model_id: If provided, filters the models by this model name. If None, prints all models.

        Returns:
            A string representation of the model library.
        """
        return self._smart_device.print_model_library(mode_str, model_id)

    # ── Worker loop ──────────────────────────────────────────────────────────

    def run(self) -> None:
        """Main worker loop — pass this as the ``target`` of a `threading.Thread`.

        Runs until [stop][sc_smart_device.SmartDeviceWorker.stop] is called.  Picks requests off the internal queue
        one at a time and calls `_execute_request`.
        """
        self.logger.log_message("SmartDeviceWorker started", "detailed")
        try:
            while not self.stop_event.is_set():
                try:
                    req = self._req_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                self._execute_request(req)
        finally:
            self.logger.log_message("SmartDeviceWorker shutdown complete.", "detailed")

    def stop(self) -> None:
        """Signal the worker loop to exit on the next iteration."""
        self.stop_event.set()

    # ── Internal execution ───────────────────────────────────────────────────

    def _execute_request(self, req: DeviceSequenceRequest) -> None:
        """Execute a `DeviceSequenceRequest`, running each step in order.

        Stores the `DeviceSequenceResult`, signals any waiting threads,
        fires the optional ``on_complete`` callback, and sets [wake_event][].

        Args:
            req: The request to execute.
        """
        start = time.time()
        res = DeviceSequenceResult(id=req.id, ok=False, started_ts=start)
        reinitialise_reqd = False

        try:
            res.ok = True  # Assume success; any failing step sets ok=False
            for step in req.steps:
                if req.timeout_s and (time.time() - start) > req.timeout_s:
                    res.ok = False
                    res.error = "sequence timeout"
                    break
                reinitialise_reqd = self._run_step(step)
        except (RuntimeError, TimeoutError) as exc:
            res.ok = False
            res.error = f"{type(exc).__name__}: {exc}"
        finally:
            res.finished_ts = time.time()
            with self._results_lock:
                self._results[req.id] = res

            if not res.ok:
                self.logger.log_message(
                    f"[smart-device] sequence '{req.label or req.id}' failed: {res.error}",
                    "error",
                )
                self.concurrent_error_count += 1
                if (
                    self.concurrent_error_count > self.max_device_errors
                    and self.report_critical_errors_delay
                ):
                    assert isinstance(self.report_critical_errors_delay, int)
                    self.logger.report_notifiable_issue(
                        entity="SmartDeviceWorker Sequence Runner",
                        issue_type="Sequence Failed",
                        send_delay=self.report_critical_errors_delay * 60,
                        message=str(res.error),
                    )

            # Signal waiters
            with self._events_lock:
                ev = self._result_events.pop(req.id, None)
            if ev:
                ev.set()

            # Optional callback
            if req.on_complete:
                try:
                    req.on_complete(res)
                except Exception as cb_err:  # noqa: BLE001
                    self.logger.log_message(
                        f"[smart-device] on_complete callback error: {cb_err}", "error"
                    )

            if reinitialise_reqd:
                self.reinitialise_settings()
                self.all_devices_online = True

            self.wake_event.set()

    def _run_step(self, step: DeviceStep) -> bool:
        """Execute a single `DeviceStep` with retries.

        Args:
            step: The step to execute.

        Returns:
            ``True`` if re-initialisation of the smart-device is required
            (i.e. all devices are now online after previously having been
            partially offline).

        Raises:
            TimeoutError: If the step times out on every retry attempt.
            RuntimeError: If a non-recoverable error occurs.
        """
        attempt = 0
        reinitialise_reqd = False

        while attempt <= step.retries and not self.stop_event.is_set():
            try:
                if step.kind == StepKind.SLEEP:
                    seconds = float(step.params.get("seconds", 0))
                    self.logger.log_message(
                        f"SmartDeviceWorker sleeping for {seconds} seconds", "debug"
                    )
                    time.sleep(seconds)

                elif step.kind == StepKind.CHANGE_OUTPUT:
                    output_identity = step.params["output_identity"]
                    state = bool(step.params.get("state", True))
                    self.logger.log_message(
                        f"SmartDeviceWorker changing output {output_identity} to state {state}",
                        "debug",
                    )
                    result, did_change = self._smart_device.change_output(output_identity, state)
                    if not result:
                        msg = f"change_output failed for {output_identity}"
                        raise TimeoutError(msg)  # noqa: TRY301
                    if not did_change:
                        self.logger.log_message(
                            f"Requested changing output {output_identity} to {state} "
                            "but it was already in that state.",
                            "warning",
                        )

                elif step.kind == StepKind.REFRESH_STATUS:
                    reinitialise_reqd = self._refresh_all_status()

                elif step.kind == StepKind.GET_LOCATION:
                    device_identity = step.params["device_identity"]
                    self.logger.log_message(
                        f"SmartDeviceWorker getting location info for device {device_identity}",
                        "debug",
                    )
                    loc_info = self._smart_device.get_device_location(device_identity)
                    if loc_info:
                        device = self._smart_device.get_device(device_identity)
                        device_name = device.get("Name", str(device_identity))
                        with self._lookup_lock:
                            self._location_data[device_name] = copy.deepcopy(loc_info)

                else:
                    msg = f"Unknown step kind: {step.kind}"
                    raise RuntimeError(msg)  # noqa: TRY301

            except TimeoutError as exc:
                attempt += 1
                if attempt <= step.retries:
                    time.sleep(step.retry_backoff_s * attempt)
                else:
                    timeout_msg = f"Step '{step.kind}' timed out after {attempt} attempts: {exc}"
                    raise TimeoutError(timeout_msg) from exc
            except RuntimeError as exc:
                runtime_msg = f"Step '{step.kind}' threw RuntimeError — skipping retries: {exc}"
                raise RuntimeError(runtime_msg) from exc
            else:
                return reinitialise_reqd

        return reinitialise_reqd

    def _refresh_all_status(self) -> bool:
        """Refresh status for every device and update the snapshot.

        Iterates over all devices via [devices][sc_smart_device.SCSmartDevice.devices]
        and calls [get_device_status][sc_smart_device.SCSmartDevice.get_device_status] for each.

        Returns:
            ``True`` if all devices are now online after having previously been
            partially offline (signals caller to reinitialise settings).

        Raises:
            TimeoutError: If a non-expected-offline device times out.
            RuntimeError: If a device raises a non-timeout error.
        """
        reinitialise_reqd = False
        offline_device = False

        for device in self._smart_device.devices:
            device_id = device.get("ID")
            device_label = device.get("Label") or device.get("Name", str(device_id))
            expect_offline = device.get("ExpectOffline", False)

            try:
                # Pass the ID (int) rather than the normalized dict, because the
                # dict snapshot returned by self._smart_device.devices lacks the
                # ObjectType field needed for provider routing.
                if not self._smart_device.get_device_status(device_id) and not expect_offline:  # pyright: ignore[reportArgumentType]
                    self.logger.log_message(
                        f"Failed to refresh status for device {device_label} — device offline.",
                        "error",
                    )
            except TimeoutError as exc:
                if expect_offline:
                    self.logger.log_message(
                        f"Device {device_label} is offline as expected.", "detailed"
                    )
                else:
                    error_msg = (
                        f"Failed to refresh status for device {device_label} — device offline."
                    )
                    self.logger.log_message(error_msg, "error")
                    raise TimeoutError(error_msg) from exc
            except RuntimeError as exc:
                error_msg = f"Error refreshing status for device {device_label}: {exc}"
                self.logger.log_message(error_msg, "error")
                raise RuntimeError(error_msg) from exc
            else:
                # Check online flag on the live device (not the snapshot)
                live_device = self._smart_device.get_device(device_id)  # pyright: ignore[reportArgumentType]
                if not live_device.get("Online", False):
                    offline_device = True

        # If all devices are now online but weren't before, signal reinitialisation
        if not offline_device and not self.all_devices_online:
            self.logger.log_message(
                "All smart devices are now online — requesting reinitialise settings.", "detailed"
            )
            reinitialise_reqd = True

        self._save_latest_status()
        return reinitialise_reqd

    def _save_latest_status(self) -> None:
        """Capture a frozen [SmartDeviceView][sc_smart_device.SmartDeviceView] snapshot."""
        with self._lookup_lock:
            self._latest_status = self._smart_device.get_view()
