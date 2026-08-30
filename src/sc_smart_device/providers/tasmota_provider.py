"""TasmotaProvider — hardware provider for Tasmota ESP32/ESP8266 smart devices.

Supports outputs, energy meters, and temperature probes via the Tasmota HTTP API.
Inputs and webhooks are NOT supported (use MQTT for event-driven integration).

API convention: ``GET http://<host>/cm?cmnd=<command>``
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn
from urllib.parse import quote

import requests
from sc_foundation import SCCommon, SCLogger

from sc_smart_device.models.capabilities import DeviceCapability
from sc_smart_device.models.smart_device_status import SmartDeviceStatus
from sc_smart_device.providers.base_provider import BaseProvider

if TYPE_CHECKING:
    from pathlib import Path

TASMOTA_MODEL = "Tasmota"


class TasmotaProvider(BaseProvider):
    """Hardware provider for Tasmota devices.

    Only processes entries in the ``Devices:`` list where ``Model: Tasmota``.
    All other device entries are silently ignored.
    """

# ── Construction ─────────────────────────────────────────────────────────────

    def __init__(self, logger: SCLogger) -> None:
        self.logger = logger

        # Mutable internal state
        self._devices: list[dict] = []
        self._outputs: list[dict] = []
        self._meters: list[dict] = []
        self._temp_probes: list[dict] = []

        # Runtime settings (overwritten by initialize_settings)
        self.allow_debug_logging = False
        self.response_timeout = 5
        self.retry_count = 1
        self.retry_delay = 2
        self.ping_allowed = True
        self.simulation_file_folder: Path | None = None

        self.provider_name = "tasmota"

    @staticmethod
    def _raise_runtime_error(message: str) -> NoReturn:
        raise RuntimeError(message)

# ── BaseProvider contract ─────────────────────────────────────────────────────

    def initialize_settings(self, provider_config: dict, refresh_status: bool = True) -> None:
        """Load config, build device list for Tasmota devices only, optionally refresh state."""
        self._add_devices_from_config(provider_config)
        if not self._devices:
            return  # nothing to do if no Tasmota devices in config
        self.is_device_online()
        if refresh_status:
            self.refresh_all_device_statuses()
        self._log_debug("TasmotaProvider initialized successfully.")

    def get_device_status(self, device_identity: dict | int | str) -> bool:
        return self._get_device_status(device_identity)

    def refresh_all_device_statuses(self) -> None:
        for device in self._devices:
            try:
                self._get_device_status(device)
            except RuntimeError as e:
                self.logger.log_message(
                    f"Error refreshing Tasmota status for {device.get('Label')}: {e}", "error"
                )
                raise

    def set_output(self, output_identity: dict | int | str, new_state: bool) -> tuple[bool, bool]:
        return self._change_output(output_identity, new_state)

    def get_device_location(self, _device_identity: dict | int | str) -> dict | None:  # noqa: PLR6301
        return None  # Tasmota does not expose location/timezone info

    def start_services(self) -> None:
        pass  # No background services needed

    def stop_services(self) -> None:
        pass

    def get_normalized_status(self) -> SmartDeviceStatus:
        return SmartDeviceStatus(
            devices=[self._normalize_device(d) for d in self._devices],
            inputs=[],  # Tasmota inputs not supported
            outputs=[self._normalize_component(c) for c in self._outputs],
            meters=[self._normalize_component(c) for c in self._meters],
            temp_probes=[self._normalize_component(c) for c in self._temp_probes],
        )

    def get_device(self, device_identity: dict | int | str) -> dict:
        """Return the internal mutable device dict for the given identity.

        Raises:
            RuntimeError: If no matching Tasmota device is found.
        """
        if isinstance(device_identity, dict):
            if device_identity.get("ObjectType") == "device":
                # Re-validate by ID so a dict from a different provider is rejected
                device_id = device_identity.get("ID")
                for device in self._devices:
                    if device["ID"] == device_id:
                        return device
                msg = f"Device with ID {device_id!r} not found in TasmotaProvider."
                raise RuntimeError(msg)
            return self.get_device(device_identity["DeviceID"])
        for device in self._devices:
            if device["ID"] == device_identity or device["Name"] == device_identity:
                return device
        msg = f"Device {device_identity!r} not found."
        raise RuntimeError(msg)

    def get_device_component(
        self,
        component_type: str,
        component_identity: int | str,
        use_index: bool | None = None,
    ) -> dict:
        """Return the internal mutable component dict.

        Raises:
            RuntimeError: If ``component_type`` is ``"input"`` (unsupported), invalid, or not found.
        """  # noqa: DOC502
        if component_type == "input":
            self._raise_runtime_error("Tasmota devices do not support input components.")
        lists: dict[str, list[dict]] = {
            "output": self._outputs,
            "meter": self._meters,
            "temp_probe": self._temp_probes,
        }
        if component_type not in lists:
            self._raise_runtime_error(f"Invalid component type {component_type!r}.")
        for component in lists[component_type]:
            if use_index and component["ComponentIndex"] == component_identity:
                return component
            if component["ID"] == component_identity or component["Name"] == component_identity:
                return component
        self._raise_runtime_error(f"{component_type} {component_identity!r} not found.")
        return None

    def is_device_online(self, device_identity: dict | int | str | None = None) -> bool:
        found_offline = False
        try:
            selected = None
            if device_identity is not None:
                selected = self.get_device(device_identity)
            for idx, device in enumerate(self._devices):
                if device["Simulate"] or not self.ping_allowed:
                    device["Online"] = True
                elif selected is None or selected["Index"] == idx:
                    online = SCCommon.ping_host(device["Hostname"], self.response_timeout)
                    device["Online"] = online
                    if not online:
                        found_offline = True
                    self._log_debug(f"{device['Label']} is {'online' if online else 'offline'}")
        except RuntimeError as e:
            raise RuntimeError(e) from e
        return not found_offline

    def does_device_have_webhooks(self, _device: dict) -> bool:  # noqa: PLR6301
        return False  # Tasmota webhooks not supported

    def install_webhook(
        self,
        _event: str,
        _component: dict,
        _url: str | None = None,
        _additional_payload: dict | None = None,
    ) -> None:
        self._raise_runtime_error("Tasmota devices do not support webhooks.")

    def pull_webhook_event(  # noqa: PLR6301
        self,
        _device_id: int | None = None,
        _device_name: str | None = None,
        _component_id: int | None = None,
        _component_name: str | None = None,
    ) -> dict | None:
        return None

    def print_model_library(self, _mode_str: str = "brief", _model_id: str | None = None) -> str:  # noqa: PLR6301
        lines = ["Tasmota Model Library:"]
        lines.append("  Tasmota")
        return "\n".join(lines)


    def print_device_status(self, device_identity: int | str | None = None) -> str:
        device_index = None
        return_str = ""
        try:
            if device_identity is not None:
                device_index = self.get_device(device_identity)["Index"]
            for idx, device in enumerate(self._devices):
                if device_index is not None and device_index != idx:
                    continue
                return_str += f"{device['Name']} (ID: {device['ID']}) — {'online' if device['Online'] else 'offline'}\n"
                return_str += f"  Model: {TASMOTA_MODEL}  Firmware: {device['Firmware']}\n"
                return_str += f"  Simulation: {device['Simulate']}\n"
                return_str += f"  Hostname: {device['Hostname']}:{device['Port']}\n"
                return_str += f"  ExpectOffline: {device['ExpectOffline']}\n"
                for ck in device.get("customkeylist", []):
                    return_str += f"  {ck}: {device[ck]}\n"
                return_str += f"  Outputs ({device['Outputs']}):\n"
                for out in self._outputs:
                    if out["DeviceIndex"] == idx:
                        custom = ", ".join(f"{k}: {out[k]}" for k in out.get("customkeylist", []))
                        return_str += (
                            f"    [{out['ComponentIndex']}] id={out['ID']} name={out['Name']!r} state={out['State']}"
                        )
                        return_str += (f", {custom}" if custom else "") + "\n"
                return_str += f"  Meters ({device['Meters']}):\n"
                for m in self._meters:
                    if m["DeviceIndex"] == idx:
                        custom = ", ".join(f"{k}: {m[k]}" for k in m.get("customkeylist", []))
                        return_str += (
                            f"    [{m['ComponentIndex']}] id={m['ID']} name={m['Name']!r} "
                            f"power={m['Power']}W voltage={m['Voltage']}V energy={m['Energy']}Wh"
                        )
                        return_str += (f", {custom}" if custom else "") + "\n"
                return_str += f"  TempProbes ({device['TempProbes']}):\n"
                for tp in self._temp_probes:
                    if tp["DeviceIndex"] == idx:
                        return_str += (
                            f"    [{tp['ComponentIndex']}] id={tp['ID']} name={tp['Name']!r} "
                            f"temp={tp['Temperature']}°C\n"
                        )
                return_str += f"  MAC: {device['MacAddress']}  Uptime: {device['Uptime']}\n"
                return_str += f"  TotalPower: {device['TotalPower']}W  TotalEnergy: {device['TotalEnergy']}Wh\n"
                return_str += "  Webhooks: not supported\n"
        except RuntimeError as e:
            raise RuntimeError(e) from e
        return return_str.strip()

    def get_device_information(
        self, device_identity: dict | int | str, refresh_status: bool = False
    ) -> dict:
        try:
            device = self.get_device(device_identity)
            if refresh_status:
                self._get_device_status(device)
        except RuntimeError as e:
            raise RuntimeError(e) from e
        idx = device["Index"]
        info = device.copy()
        info["Inputs"] = []  # Tasmota inputs not supported
        info["Outputs"] = [c for c in self._outputs if c["DeviceIndex"] == idx]
        info["Meters"] = [c for c in self._meters if c["DeviceIndex"] == idx]
        info["TempProbes"] = [c for c in self._temp_probes if c["DeviceIndex"] == idx]
        return info

# ── Config loading ────────────────────────────────────────────────────────────

    def _add_devices_from_config(self, settings: dict) -> None:
        self.allow_debug_logging = settings.get("AllowDebugLogging", False)
        self.response_timeout = settings.get("ResponseTimeout", self.response_timeout)
        self.retry_count = settings.get("RetryCount", self.retry_count)
        self.retry_delay = settings.get("RetryDelay", self.retry_delay)
        self.ping_allowed = settings.get("PingAllowed", True)

        relative_folder = settings.get("SimulationFileFolder", "simulation_files")
        self.simulation_file_folder = SCCommon.select_folder_location(relative_folder, create_folder=True)

        self._devices.clear()
        self._outputs.clear()
        self._meters.clear()
        self._temp_probes.clear()

        for device_cfg in settings.get("Devices", []):
            if str(device_cfg.get("Model")) == TASMOTA_MODEL:
                self._add_device(device_cfg)

    def _add_device(self, device_config: dict) -> None:
        # Inputs block is explicitly forbidden for Tasmota devices
        if device_config.get("Inputs"):
            self._raise_runtime_error(
                f"Tasmota device {device_config.get('Name', 'Unknown')!r} must not have an "
                "'Inputs:' block — Tasmota input/button events are not supported."
            )

        device_index = len(self._devices)
        device_id = device_config.get("ID", device_index + 1)
        client_name = device_config.get("Name") or f"Device {device_id}"

        outputs_cfg: list[dict] = device_config.get("Outputs") or []
        meters_cfg: list[dict] = device_config.get("Meters") or []
        temp_probes_cfg: list[dict] = device_config.get("TempProbes") or []

        simulate = bool(device_config.get("Simulate"))
        hostname = device_config.get("Hostname")

        if not simulate and not hostname:
            self._raise_runtime_error(f"Tasmota device {client_name!r} has no Hostname configured.")
        if hostname and not SCCommon.is_valid_hostname(hostname):
            self._raise_runtime_error(
                f"Tasmota device {client_name!r} has invalid hostname {hostname!r}."
            )

        label = f"{client_name} (ID: {device_id})"
        new_device: dict = {
            "Index": device_index,
            "Model": TASMOTA_MODEL,
            "Name": client_name,
            "ID": device_id,
            "ObjectType": "device",
            "Simulate": simulate,
            "SimulationFile": None,
            "ExpectOffline": bool(device_config.get("ExpectOffline")),
            "Hostname": hostname,
            "Port": device_config.get("Port", 80),
            "Online": False,
            "MacAddress": None,
            "Uptime": None,
            "Firmware": None,
            "Temperature": None,
            "Inputs": 0,
            "Outputs": len(outputs_cfg),
            "Meters": len(meters_cfg),
            "TempProbes": len(temp_probes_cfg),
            "TotalPower": 0.0,
            "TotalEnergy": 0.0,
            "Label": label,
            "customkeylist": [],
        }

        for existing in self._devices:
            if existing["Name"] == new_device["Name"]:
                self._raise_runtime_error(f"Device Name {new_device['Name']!r} must be unique.")
            if existing["ID"] == new_device["ID"]:
                self._raise_runtime_error(f"Device ID {new_device['ID']} must be unique.")

        if simulate:
            fname = "".join(c if c.isalnum() else "_" for c in client_name) + ".json"
            new_device["SimulationFile"] = (
                self.simulation_file_folder / fname  # pyright: ignore[reportOptionalOperand]
            )

        # Absorb custom keys from config (anything not already in the device dict)
        skip_keys = {"Outputs", "Meters", "TempProbes", "Inputs", "Tasmota"}
        known_keys = set(new_device.keys())
        for key, value in device_config.items():
            if key not in known_keys and key not in skip_keys:
                new_device[key] = value
                new_device["customkeylist"].append(key)

        self._devices.append(new_device)

        self._add_components(device_index, "output", outputs_cfg)
        self._add_components(device_index, "meter", meters_cfg)
        self._add_components(device_index, "temp_probe", temp_probes_cfg)

        self._import_device_information_from_json(new_device, create_if_no_file=True)
        self._log_debug(f"Added Tasmota device {label}.")

    def _add_components(
        self, device_index: int, component_type: str, component_config: list[dict]
    ) -> None:
        type_map: dict[str, tuple[list[dict], str]] = {
            "output": (self._outputs, "Output"),
            "meter": (self._meters, "Meter"),
            "temp_probe": (self._temp_probes, "TempProbe"),
        }
        storage, prefix = type_map[component_type]
        device = self._devices[device_index]

        for comp_idx, cfg in enumerate(component_config):
            comp_id = cfg.get("ID", len(storage) + 1)
            new_comp: dict = {
                "DeviceIndex": device_index,
                "DeviceID": device["ID"],
                "ComponentIndex": comp_idx,
                "ObjectType": component_type,
                "ID": comp_id,
                "Name": cfg.get("Name", f"{prefix} {comp_id}"),
                "Webhooks": False,
                "customkeylist": [],
            }

            if component_type == "output":
                new_comp["State"] = False
                new_comp["Temperature"] = None
            elif component_type == "meter":
                new_comp["Power"] = None
                new_comp["Voltage"] = None
                new_comp["Current"] = None
                new_comp["PowerFactor"] = None
                new_comp["Energy"] = None
            elif component_type == "temp_probe":
                new_comp["Temperature"] = None
                new_comp["LastReadingTime"] = None

            for existing in storage:
                if existing["Name"] == new_comp["Name"]:
                    self._raise_runtime_error(f"{prefix} Name {new_comp['Name']!r} must be unique.")
                if existing["ID"] == new_comp["ID"]:
                    self._raise_runtime_error(f"{prefix} ID {new_comp['ID']} must be unique.")

            known_comp_keys = set(new_comp.keys())
            for key, value in cfg.items():
                if key not in known_comp_keys:
                    new_comp[key] = value
                    new_comp["customkeylist"].append(key)

            storage.append(new_comp)

# ── Device status ─────────────────────────────────────────────────────────────

    def _get_device_status(self, device_identity: dict | int | str) -> bool:
        device = self.get_device(device_identity)

        if device["Simulate"]:
            self._import_device_information_from_json(device, create_if_no_file=True)
            return True

        try:
            result, result_data = self._http_request(device, "Status 0")
        except TimeoutError as e:
            self.logger.log_message(f"Timeout getting Tasmota status for {device['Label']}: {e}", "error")
            raise
        except RuntimeError as e:
            self.logger.log_message(f"Error getting Tasmota status for {device['Label']}: {e}", "error")
            raise

        if not result:
            self._set_device_outputs_off(device)
            return False

        try:
            device["Online"] = True
            device["MacAddress"] = result_data.get("StatusNET", {}).get("Mac")
            device["Uptime"] = result_data.get("StatusPRM", {}).get("Uptime")
            device["Firmware"] = result_data.get("StatusFWR", {}).get("Version")

            status_sts = result_data.get("StatusSTS", {})
            num_outputs = device["Outputs"]

            for out in self._outputs:
                if out["DeviceIndex"] != device["Index"]:
                    continue
                ci = out["ComponentIndex"]
                if num_outputs == 1:
                    state_str = status_sts.get("POWER", "OFF")
                else:
                    state_str = status_sts.get(f"POWER{ci + 1}", "OFF")
                out["State"] = state_str.upper() == "ON"

            # Energy from StatusSNS.ENERGY
            status_sns = result_data.get("StatusSNS", {})
            energy_block = status_sns.get("ENERGY", {})
            for meter in self._meters:
                if meter["DeviceIndex"] != device["Index"]:
                    continue
                meter["Power"] = energy_block.get("Power") if energy_block else None
                meter["Voltage"] = energy_block.get("Voltage") if energy_block else None
                meter["Current"] = energy_block.get("Current") if energy_block else None
                meter["PowerFactor"] = energy_block.get("Factor") if energy_block else None
                total_kwh = energy_block.get("Total", 0) if energy_block else 0
                meter["Energy"] = float(total_kwh) * 1000.0  # kWh → Wh

            # Temperature probes (e.g. DS18B20, AM2301)
            for tp in self._temp_probes:
                if tp["DeviceIndex"] != device["Index"]:
                    continue
                for sensor_data in status_sns.values():
                    if isinstance(sensor_data, dict) and "Temperature" in sensor_data:
                        tp["Temperature"] = sensor_data["Temperature"]
                        break

            self._calculate_device_energy_totals(device)

        except (KeyError, TypeError, ValueError) as e:
            msg = f"Error parsing Tasmota status for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e

        return True

# ── Output control ────────────────────────────────────────────────────────────

    def _change_output(
        self, output_identity: dict | int | str, new_state: bool
    ) -> tuple[bool, bool]:
        try:
            if isinstance(output_identity, dict):
                output = self.get_device_component("output", output_identity["ID"])
            else:
                output = self.get_device_component("output", output_identity)
            device = self.get_device(output["DeviceID"])
        except RuntimeError as e:
            raise RuntimeError(e) from e

        if not device.get("Online", False):
            return False, False

        current_state = output.get("State", False)
        if current_state == new_state:
            return True, False

        if device["Simulate"]:
            output["State"] = new_state
            self._calculate_device_energy_totals(device)
            self._export_device_information_to_json(device)
            return True, True

        # Live device
        ci = output["ComponentIndex"]
        num_outputs = device["Outputs"]
        state_arg = "1" if new_state else "0"
        cmd = f"Power {state_arg}" if num_outputs == 1 else f"Power{ci + 1} {state_arg}"

        try:
            result, result_data = self._http_request(device, cmd)
        except (TimeoutError, RuntimeError) as e:
            msg = f"Error setting Tasmota output {output['Name']!r}: {e}"
            raise RuntimeError(msg) from e

        if not result:
            return False, False

        # Confirm from response
        if num_outputs == 1:
            state_str = result_data.get("POWER", "OFF")
        else:
            state_str = result_data.get(f"POWER{ci + 1}", "OFF")
        output["State"] = state_str.upper() == "ON"
        self._calculate_device_energy_totals(device)
        return True, True

    def _set_device_outputs_off(self, device: dict) -> None:
        device["Online"] = False
        for out in self._outputs:
            if out["DeviceIndex"] == device["Index"]:
                out["State"] = False

    def _calculate_device_energy_totals(self, device: dict) -> None:
        total_power = 0.0
        total_energy = 0.0
        for meter in self._meters:
            if meter["DeviceIndex"] != device["Index"]:
                continue
            if meter.get("Power") is not None:
                total_power += float(meter["Power"])
            if meter.get("Energy") is not None:
                total_energy += float(meter["Energy"])
        device["TotalPower"] = total_power
        device["TotalEnergy"] = total_energy

# ── HTTP transport ────────────────────────────────────────────────────────────

    def _http_request(self, device: dict, command: str) -> tuple[bool, dict]:
        """Send a Tasmota HTTP command.

        Returns:
            Tuple of ``(success, response_dict)``.

        Raises:
            TimeoutError: If the request times out.
        """
        url = f"http://{device['Hostname']}:{device['Port']}/cm?cmnd={quote(command)}"
        try:
            response = requests.get(url, timeout=self.response_timeout)
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.Timeout as e:
            msg = f"Tasmota request timed out: {url}"
            raise TimeoutError(msg) from e
        except requests.exceptions.ConnectionError as e:
            self.logger.log_message(
                f"Connection error for {device['Label']}: {e}", "error"
            )
            return False, {}
        except requests.exceptions.HTTPError as e:
            self.logger.log_message(f"HTTP error for {device['Label']}: {e}", "error")
            return False, {}
        except (ValueError, json.JSONDecodeError) as e:
            self.logger.log_message(
                f"JSON decode error for {device['Label']}: {e}", "error"
            )
            return False, {}

# ── Simulation I/O ────────────────────────────────────────────────────────────

    @staticmethod
    def _get_simulation_file_path(device: dict) -> Path:
        return device["SimulationFile"]  # type: ignore[return-value]

    def _import_device_information_from_json(self, device: dict, create_if_no_file: bool) -> bool:  # noqa: PLR0912
        if not device.get("Simulate"):
            return False
        file_path = self._get_simulation_file_path(device)
        if not file_path.exists():
            if create_if_no_file:
                return self._export_device_information_to_json(device)
            msg = f"Simulation file {file_path} not found."
            raise RuntimeError(msg)
        try:  # noqa: PLR1702
            with file_path.open("r", encoding="utf-8") as f:
                device_info = json.load(f)
            device["Online"] = True
            for key in ("MacAddress", "Uptime", "Firmware", "Temperature"):
                if key in device_info and key in device:
                    device[key] = device_info[key]
            device_index = device["Index"]
            for imported_out in device_info.get("Outputs") or []:
                for out in self._outputs:
                    if (
                        out["DeviceIndex"] == device_index
                        and out["ComponentIndex"] == imported_out.get("ComponentIndex")
                    ):
                        out["State"] = imported_out.get("State", False)
            for imported_meter in device_info.get("Meters") or []:
                for meter in self._meters:
                    if (
                        meter["DeviceIndex"] == device_index
                        and meter["ComponentIndex"] == imported_meter.get("ComponentIndex")
                    ):
                        for key in ("Power", "Voltage", "Current", "PowerFactor", "Energy"):
                            if key in imported_meter:
                                meter[key] = imported_meter[key]
            for imported_tp in device_info.get("TempProbes") or []:
                for tp in self._temp_probes:
                    if (
                        tp["DeviceIndex"] == device_index
                        and tp["ComponentIndex"] == imported_tp.get("ComponentIndex")
                    ):
                        tp["Temperature"] = imported_tp.get("Temperature")
            self._calculate_device_energy_totals(device)
        except (OSError, json.JSONDecodeError, KeyError) as e:
            msg = f"Error importing Tasmota simulation file for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            self._log_debug(f"Tasmota simulation data loaded from {file_path}")
            return True

    def _export_device_information_to_json(self, device: dict) -> bool:
        if not device.get("Simulate"):
            return False
        file_path = self._get_simulation_file_path(device)
        try:
            info = self.get_device_information(device, refresh_status=False)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False, default=str)
        except (RuntimeError, OSError) as e:
            msg = f"Error exporting Tasmota simulation file for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            self._log_debug(f"Tasmota simulation file written: {file_path}")
            return True

# ── Normalized snapshot helpers ───────────────────────────────────────────────

    @staticmethod
    def _normalize_device(device: dict) -> dict:
        caps: set[DeviceCapability] = {DeviceCapability.POLLING}
        if device.get("Outputs", 0) > 0:
            caps.add(DeviceCapability.OUTPUT_CONTROL)
            caps.add(DeviceCapability.OUTPUT_STATE)
        if device.get("Meters", 0) > 0:
            caps.add(DeviceCapability.METERING)
        if device.get("TempProbes", 0) > 0:
            caps.add(DeviceCapability.TEMPERATURE)
        if device.get("Simulate"):
            caps.add(DeviceCapability.SIMULATION)
        base = {
            "ID": device["ID"],
            "Name": device["Name"],
            "Online": device.get("Online", False),
            "Simulate": device.get("Simulate", False),
            "ExpectOffline": device.get("ExpectOffline", False),
            "Temperature": device.get("Temperature"),
            "TotalPower": device.get("TotalPower", 0.0),
            "TotalEnergy": device.get("TotalEnergy", 0.0),
            "Capabilities": caps,
        }
        internal = {"customkeylist", "Outputs", "Inputs", "Meters", "TempProbes", "Tasmota"}
        for key, value in device.items():
            if key not in base and key not in internal:
                base[key] = value
        return base

    @staticmethod
    def _normalize_component(component: dict) -> dict:
        obj_type = component.get("ObjectType", "")
        base: dict = {
            "ID": component["ID"],
            "Name": component["Name"],
            "DeviceID": component["DeviceID"],
            "ComponentIndex": component["ComponentIndex"],
            "ObjectType": obj_type,
        }
        if obj_type == "output":
            base["State"] = component.get("State", False)
            base["Temperature"] = component.get("Temperature")
        elif obj_type == "meter":
            base["Power"] = component.get("Power")
            base["Voltage"] = component.get("Voltage")
            base["Current"] = component.get("Current")
            base["PowerFactor"] = component.get("PowerFactor")
            base["Energy"] = component.get("Energy")
        elif obj_type == "temp_probe":
            base["Temperature"] = component.get("Temperature")
            base["LastReadingTime"] = component.get("LastReadingTime")
        internal = {"customkeylist"}
        for key, value in component.items():
            if key not in base and key not in internal:
                base[key] = value
        return base

# ── Helpers ───────────────────────────────────────────────────────────────────

    def _log_debug(self, message: str) -> None:
        if self.allow_debug_logging:
            self.logger.log_message(message, "debug")
