"""SCSmartDevice — the stable public API for controlling smart devices.

Client apps should only depend on this class, not on any provider directly.
"""
from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from sc_smart_device.models.smart_device_status import SmartDeviceStatus
from sc_smart_device.models.smart_device_view import SmartDeviceView
from sc_smart_device.providers.shelly_provider import ShellyProvider
from sc_smart_device.providers.tasmota_provider import TasmotaProvider

if TYPE_CHECKING:
    import threading
    from collections.abc import Iterator

    from sc_foundation import SCLogger

    from sc_smart_device.providers.base_provider import BaseProvider


class SCSmartDevice:  # noqa: PLR0904
    """Unified smart-device controller.

    Aggregates one or more hardware providers (currently `ShellyProvider`
    and `TasmotaProvider`) behind a single, stable public API.  Client
    apps should never depend on a provider class directly.
    """

    def __init__(
        self,
        logger: SCLogger,
        device_settings: dict,
        app_wake_event: threading.Event | None = None,
    ) -> None:
        """Accepts the ``SCSmartDevices:`` config block parsed from the client app's YAML file and instantiates the appropriate hardware providers.

        Signature matches the legacy ``ShellyControl`` constructor so existing
        client apps require minimal changes.

        Args:
            logger: SCLogger instance from sc-foundation.
            device_settings: The ``SCSmartDevices`` dict from the app config.
            app_wake_event: Optional threading.Event set when a webhook fires.

        Raises:
            RuntimeError: If config is invalid or the model file cannot be loaded.
        """  # noqa: DOC502
        self._logger = logger
        self._providers: list[BaseProvider] = [
            ShellyProvider(logger, app_wake_event),
            TasmotaProvider(logger),
        ]
        preprocessed = self._preprocess_config(device_settings)
        for provider in self._providers:
            provider.initialize_settings(preprocessed)
        self._validate_global_uniqueness()
        # Only Shelly has a webhook server; Tasmota start_services is a no-op
        for provider in self._providers:
            provider.start_services()

    # ── Global config validation ─────────────────────────────────────────────

    def _validate_global_uniqueness(self) -> None:
        """Verify that device IDs and names are unique across ALL providers.

        Because each provider assigns IDs independently, a misconfigured YAML
        could produce duplicate IDs across providers, causing silent mis-routing.
        This check runs once after all providers are initialised and raises a
        descriptive RuntimeError so the problem is caught at startup.

        Raises:
            RuntimeError: If any device ID or name is shared across providers,
                or if any component ID is duplicated within its type across providers.
        """
        status = self._aggregated_status()

        # Check devices
        seen_device_ids: dict[int, str] = {}
        seen_device_names: dict[str, int] = {}
        for device in status.devices:
            did, dname = device["ID"], device["Name"]
            if did in seen_device_ids:
                msg = (
                    f"Duplicate device ID {did!r}: shared by "
                    f"{seen_device_ids[did]!r} and {dname!r}."
                )
                raise RuntimeError(msg)
            if dname in seen_device_names:
                msg = f"Duplicate device Name {dname!r}: device names must be globally unique."
                raise RuntimeError(msg)
            seen_device_ids[did] = dname
            seen_device_names[dname] = did

        # Check components by type
        for label, components in (
            ("output", status.outputs),
            ("input", status.inputs),
            ("meter", status.meters),
            ("temp_probe", status.temp_probes),
        ):
            seen_ids: dict[int, str] = {}
            seen_names: dict[str, int] = {}
            for comp in components:
                cid, cname = comp["ID"], comp["Name"]
                if cid in seen_ids:
                    msg = (
                        f"Duplicate {label} ID {cid!r}: shared by "
                        f"{seen_ids[cid]!r} and {cname!r}."
                    )
                    raise RuntimeError(msg)
                if cname in seen_names:
                    msg = f"Duplicate {label} Name {cname!r}: component names must be globally unique."
                    raise RuntimeError(msg)
                seen_ids[cid] = cname
                seen_names[cname] = cid

    # ── Global config pre-processing ─────────────────────────────────────────

    @staticmethod
    def _preprocess_config(device_settings: dict) -> dict:
        """Assign globally unique IDs to every device and component before providers see the config.

        Client apps only need to supply ``Name`` (or ``ID``, or both).  This
        method fills in any missing ``ID`` values with sequentially-assigned
        integers that are guaranteed to be unique across ALL providers and ALL
        component types.

        For Shelly devices that don't list component blocks (e.g. no ``Inputs:``
        section), the method looks up the model file to find out how many
        components the hardware has and synthesises placeholder dicts so those
        components also receive globally unique IDs.

        The original ``device_settings`` dict is never mutated — a deep copy is
        returned.

        Returns:
            A deep-copied settings dict with globally unique IDs assigned.
        """
        settings = copy.deepcopy(device_settings)
        devices: list[dict] = settings.get("Devices") or []

        comp_types = ("Inputs", "Outputs", "Meters", "TempProbes")
        model_key_map = {"Inputs": "inputs", "Outputs": "outputs", "Meters": "meters"}

        # ── Phase 1: expand Shelly model-driven component lists ───────────────
        # If a Shelly device omits a component block the provider creates those
        # components automatically from the model file.  We synthesise empty dicts
        # here so Phase 2 can assign them globally unique IDs.
        for device in devices:
            if str(device.get("Model")) == "Tasmota":
                continue  # Tasmota components are always explicit in config
            counts = ShellyProvider.get_model_component_counts(str(device.get("Model", "")))
            if not counts:
                continue
            for ct, model_key in model_key_map.items():
                n = counts.get(model_key, 0)
                if n > 0 and device.get(ct) is None:
                    # Synthesise n placeholder dicts — providers will fill in names
                    device[ct] = [{} for _ in range(n)]

        # ── Phase 2: collect all explicitly-set IDs ───────────────────────────
        used_device_ids: set[int] = set()
        used_comp_ids: dict[str, set[int]] = {ct: set() for ct in comp_types}

        for device in devices:
            if device.get("ID") is not None:
                used_device_ids.add(int(device["ID"]))
            for ct in comp_types:
                for comp in device.get(ct) or []:
                    if comp.get("ID") is not None:
                        used_comp_ids[ct].add(int(comp["ID"]))

        # ── Phase 3: sequential generators that skip already-used IDs ─────────
        def _make_gen(used: set[int]) -> Iterator[int]:
            i = 1
            while True:
                if i not in used:
                    used.add(i)
                    yield i
                i += 1

        device_gen = _make_gen(used_device_ids)
        comp_gens = {ct: _make_gen(used_comp_ids[ct]) for ct in comp_types}

        # ── Phase 4: assign missing IDs ───────────────────────────────────────
        for device in devices:
            if device.get("ID") is None:
                device["ID"] = next(device_gen)
            for ct in comp_types:
                for comp in device.get(ct) or []:
                    if comp.get("ID") is None:
                        comp["ID"] = next(comp_gens[ct])

        return settings

    # ── Provider routing helpers ─────────────────────────────────────────────

    def _aggregated_status(self) -> SmartDeviceStatus:
        """Merge normalized status from all providers into one SmartDeviceStatus.

        Returns:
            Aggregated normalized status across all providers.
        """
        devices: list[dict] = []
        inputs: list[dict] = []
        outputs: list[dict] = []
        meters: list[dict] = []
        temp_probes: list[dict] = []
        for provider in self._providers:
            s = provider.get_normalized_status()
            devices.extend(s.devices)
            inputs.extend(s.inputs)
            outputs.extend(s.outputs)
            meters.extend(s.meters)
            temp_probes.extend(s.temp_probes)
        return SmartDeviceStatus(
            devices=devices,
            inputs=inputs,
            outputs=outputs,
            meters=meters,
            temp_probes=temp_probes,
        )

    def _provider_for_device(self, device_identity: dict | int | str) -> BaseProvider:
        """Return the provider that owns the given device.

        Raises:
            RuntimeError: If the device is not found in any provider.
        """
        for provider in self._providers:
            try:
                provider.get_device(device_identity)
            except RuntimeError:
                continue
            else:
                return provider
        msg = f"Device {device_identity!r} not found in any provider."
        raise RuntimeError(msg)

    def _provider_for_component(
        self,
        component_type: str,
        component_identity: int | str,
        use_index: bool | None = None,
    ) -> BaseProvider:
        """Return the provider that owns the given component.

        Raises:
            RuntimeError: If the component is not found in any provider.
        """
        for provider in self._providers:
            try:
                provider.get_device_component(component_type, component_identity, use_index)
            except RuntimeError:
                continue
            else:
                return provider
        msg = f"{component_type} {component_identity!r} not found in any provider."
        raise RuntimeError(msg)

    # ── Re-initialise ────────────────────────────────────────────────────────

    def initialize_settings(self, device_settings: dict, refresh_status: bool = True) -> None:
        """Reload device configuration without restarting the webhook server.

        Useful for hot-reloading when the YAML config file changes at runtime.

        Args:
            device_settings: Updated ``SCSmartDevices`` dict.
            refresh_status: If True (default) refresh device state after loading.
        """
        preprocessed = self._preprocess_config(device_settings)
        for provider in self._providers:
            provider.initialize_settings(preprocessed, refresh_status)
        self._validate_global_uniqueness()

    # ── Public list properties (normalized, provider-agnostic copies) ─────────

    @property
    def devices(self) -> list[dict]:
        """Normalized snapshot of all device dicts."""
        return self._aggregated_status().devices

    @property
    def inputs(self) -> list[dict]:
        """Normalized snapshot of all input component dicts."""
        return self._aggregated_status().inputs

    @property
    def outputs(self) -> list[dict]:
        """Normalized snapshot of all output component dicts."""
        return self._aggregated_status().outputs

    @property
    def meters(self) -> list[dict]:
        """Normalized snapshot of all meter component dicts."""
        return self._aggregated_status().meters

    @property
    def temp_probes(self) -> list[dict]:
        """Normalized snapshot of all temperature probe dicts."""
        return self._aggregated_status().temp_probes

    # ── Thread-safe view ────────────────────────────────────────────────────

    def get_view(self) -> SmartDeviceView:
        """Return a frozen, indexed, thread-safe snapshot of current device state.

        Use this in multi-threaded applications (e.g. alongside SmartDeviceWorker)
        to safely read device state without holding a lock.

        Returns:
            SmartDeviceView built from the current normalized status.
        """
        return SmartDeviceView(self._aggregated_status())

    # ── Live internal lookups (mutable dicts, all fields) ────────────────────
    # These return references into the provider's internal state so that the
    # existing pattern of "hold a reference, read State after refresh" continues
    # to work.  Provider-internal fields (Protocol, DeviceIndex, etc.) are
    # accessible here but not exposed through the normalized properties above.

    def get_device(self, device_identity: dict | int | str) -> dict:
        """Returns the device index for a given device ID or name.

        For device_identity you can pass:
        - A device object (dict) to retrieve it directly.
        - The device ID (int) to look it up by ID.
        - The device name (str) to look it up by name.
        - A component object (dict), which will return the parent device.

        Args:
            device_identity (dict | int | str): The identifier for the device.

        Returns:
            device (dict): The device object if found.

        Raises:
            RuntimeError: If the device is not found in the list of devices.
        """  # noqa: DOC502
        return self._provider_for_device(device_identity).get_device(device_identity)

    def get_device_component(
        self,
        component_type: str,
        component_identity: int | str,
        use_index: bool | None = None,
    ) -> dict:
        """Returns a device component's index for a given component ID or name.

        Args:
            component_type (str): The type of component to retrieve ('input', 'output', 'meter' or 'temp_probe').
            component_identity (int | str): The ID or name of the component to retrieve.
            use_index (bool | None): If True, lookup by index instead of ID or name.

        Returns:
            component(dict): The component index if found.

        Raises:
            RuntimeError: If the component is not found in the list.
        """  # noqa: DOC502
        return self._provider_for_component(
            component_type, component_identity, use_index
        ).get_device_component(component_type, component_identity, use_index)

    # ── Convenience component shorthand ─────────────────────────────────────

    def get_output(self, output_identity: int | str) -> dict:
        """Shorthand for ``get_device_component("output", output_identity)``.

        Returns:
            The internal output component dict.
        """
        return self.get_device_component("output", output_identity)

    def get_input(self, input_identity: int | str) -> dict:
        """Shorthand for ``get_device_component("input", input_identity)``.

        Returns:
            The internal input component dict.
        """
        return self.get_device_component("input", input_identity)

    def get_meter(self, meter_identity: int | str) -> dict:
        """Shorthand for ``get_device_component("meter", meter_identity)``.

        Returns:
            The internal meter component dict.
        """
        return self.get_device_component("meter", meter_identity)

    def get_temp_probe(self, probe_identity: int | str) -> dict:
        """Shorthand for ``get_device_component("temp_probe", probe_identity)``.

        Returns:
            The internal temperature probe component dict.
        """
        return self.get_device_component("temp_probe", probe_identity)

    # ── Device status ────────────────────────────────────────────────────────

    def is_device_online(self, device_identity: dict | int | str | None = None) -> bool:
        """See if a device is alive by pinging it.

        Returns the result and updates the device's online status. If we are in simulation mode, always returns True.

        Args:
            device_identity (Optional (dict | int | str | None), optional): The actual device object, device component object,
                        device ID or device name of the device to check. If None, checks all devices.

        Returns:
            result (bool): True if the device is online, False otherwise. If all devices are checked, returns True if all device are online.

        Raises:
            RuntimeError: If the device is not found in the list of devices.
        """  # noqa: DOC502
        if device_identity is not None:
            return self._provider_for_device(device_identity).is_device_online(device_identity)
        # Check all providers
        return all(provider.is_device_online() for provider in self._providers)

    def get_device_status(self, device_identity: dict | int | str) -> bool:
        """Gets the status of a device.

        Args:
            device_identity (dict | int | str): A device dict, or the ID or name of the device to check.

        Returns:
            result (bool): True if the device is online, False otherwise.

        Raises:
            RuntimeError: If the device is not found in the list of devices or if there is an error getting the status.
            TimeoutError: If the device is online (ping) but the request times out while getting the device status.
        """  # noqa: DOC502
        return self._provider_for_device(device_identity).get_device_status(device_identity)

    def refresh_all_device_statuses(self) -> None:
        """Refreshes the status of all devices across all providers.

        Raises:
            RuntimeError: If there is an error getting the status of any device.
        """  # noqa: DOC502
        for provider in self._providers:
            provider.refresh_all_device_statuses()

    def refresh(self) -> None:
        """Alias for [refresh_all_device_statuses][sc_smart_device.SCSmartDevice.refresh_all_device_statuses]."""
        self.refresh_all_device_statuses()

    # ── Output control ───────────────────────────────────────────────────────

    def change_output(
        self, output_identity: dict | int | str, new_state: bool
    ) -> tuple[bool, bool]:
        """Change an output relay to the requested state.

        Args:
            output_identity: Output dict, ID, or name.
            new_state: True to turn on, False to turn off.

        Returns:
            ``(success, did_change)`` — success is False when the device is
            offline; did_change is False when the output was already in the
            requested state.

        Raises:
            RuntimeError: There was an error changing the device output state.
            TimeoutError: If the device is online (ping) but the state change request times out.

        """  # noqa: DOC502
        if isinstance(output_identity, dict):
            provider = self._provider_for_device(output_identity.get("DeviceID", -1))
        else:
            provider = self._provider_for_component("output", output_identity)
        return provider.set_output(output_identity, new_state)

    # ── Webhooks ─────────────────────────────────────────────────────────────

    def install_webhook(
        self,
        event: str,
        component: dict,
        url: str | None = None,
        additional_payload: dict | None = None,
    ) -> None:
        """Install a webhook on a device component.

        See https://spello-consulting.github.io/sc-smart-device/shelly_webhooks/ for details on supported events and payloads.

        Args:
            event: Shelly event name, e.g. ``"input.toggle_on"``.
            component: Component dict (from [get_device_component][sc_smart_device.SCSmartDevice.get_device_component]).
            url: Override the callback URL. Auto-constructed if None.
            additional_payload: Extra query-string parameters to include.
        """
        self._provider_for_device(component.get("DeviceID", -1)).install_webhook(
            event, component, url, additional_payload
        )

    def pull_webhook_event(self) -> dict | None:
        """Return the oldest queued webhook event and remove it from the queue.

        Returns:
            Event dict with keys ``timestamp``, ``Event``, ``Device``,
            ``Component``, etc.; or None if the queue is empty.
        """
        for provider in self._providers:
            event = provider.pull_webhook_event()
            if event is not None:
                return event
        return None

    def does_device_have_webhooks(self, device: dict) -> bool:
        """Return True if any component of the device has webhooks enabled."""
        try:
            return self._provider_for_device(device.get("ID", -1)).does_device_have_webhooks(device)
        except RuntimeError:
            return False

    # ── Device location ──────────────────────────────────────────────────────

    def get_device_location(self, device_identity: dict | int | str) -> dict | None:
        """Gets the timezone and location of a device if available.

        Returns a dict in the following format:
           "tz": "Europe/Sofia",
           "lat": 42.67236,
           "lon": 23.38738

        Args:
            device_identity (dict | int | str): A device dict, or the ID or name of the device to check.

        Returns:
            location (dict | None): A dictionary containing the timezone and location of the device, or None if not available.

        Raises:
            RuntimeError: If the device is not found in the list of devices or if there is an error getting the status.
            TimeoutError: If the device is online (ping) but the request times out while getting the device status.
        """  # noqa: DOC502
        return self._provider_for_device(device_identity).get_device_location(device_identity)

    # ── Device information ───────────────────────────────────────────────────

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
        return self._provider_for_device(device_identity).get_device_information(
            device_identity, refresh_status
        )

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def print_device_status(self, device_identity: int | str | None = None) -> str:
        """Prints the status of a device or all devices.

        Args:
            device_identity (Optional (int | str | None), optional): The ID or name of the device to check. If None, checks all devices.

        Returns:
            device_info (str): A string representation of the device status.

        Raises:
            RuntimeError: If the device is not found in the list of devices.
        """  # noqa: DOC502
        if device_identity is not None:
            return self._provider_for_device(device_identity).print_device_status(device_identity)
        # Aggregate from all providers
        parts = [p.print_device_status() for p in self._providers if p.get_normalized_status().devices]
        return "\n".join(part for part in parts if part)

    def print_model_library(self, mode_str: str = "brief", model_id: str | None = None, provider_id: str | None = None) -> str:
        """Prints the model library for all providers that have one.

        Args:
            mode_str (str, optional): The mode of printing. Can be "brief" or "detailed". Defaults to "brief".
            model_id (Optional (str), optional): If provided, filters the models by this model name. If None, prints all models.
            provider_id (Optional (str), optional): If provided, filters the models by this provider name ('shelly' or 'tasmota'). If None, prints models from all providers.

        Returns:
            library_info (str): A string representation of the model library.
        """
        provider_id = provider_id.lower() if provider_id else None
        parts = [p.print_model_library(mode_str, model_id) for p in self._providers if (not provider_id or getattr(p, "provider_name", "").lower() == provider_id)]
        return "\n".join(part for part in parts if part)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Stop all provider services and release resources.

        Call this when the client application is terminating.
        """
        for provider in self._providers:
            provider.stop_services()
