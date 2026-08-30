"""ShellyProvider — hardware provider for Shelly smart switch devices."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from http.server import ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import NoReturn

import requests
from sc_foundation import DateHelper, SCCommon, SCLogger

from sc_smart_device.models.capabilities import DeviceCapability
from sc_smart_device.models.smart_device_status import SmartDeviceStatus
from sc_smart_device.providers.base_provider import BaseProvider
from sc_smart_device.webhooks.shelly_webhook_server import _ShellyWebhookHandler

SHELLY_MODEL_FILE = "shelly_models.json"
DEFAULT_WEBHOOK_LISTEN = "0.0.0.0"  # noqa: S104
DEFAULT_WEBHOOK_CALLBACK_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_WEBHOOK_PORT = 8787
DEFAULT_WEBHOOK_PATH = "/shelly/webhook"
FIRST_TEMP_PROBE_ID = 100


class ShellyProvider(BaseProvider):
    """Shelly hardware provider.

    Owns the raw mutable device/component dicts (with all Shelly-specific fields).
    Translates Shelly API calls into the shared provider contract and produces
    normalized SmartDeviceStatus snapshots for SCSmartDevice.
    """

# ── Construction ────────────────────────────────────────────────────────────

    def __init__(self, logger: SCLogger, app_wake_event: threading.Event | None = None) -> None:
        self.logger = logger
        self._app_wake_event = app_wake_event

        # Internal mutable state (Shelly-specific fields included)
        self._devices: list[dict] = []
        self._inputs: list[dict] = []
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

        # Webhook state
        self.webhook_enabled = False
        self.webhook_callback_host = DEFAULT_WEBHOOK_CALLBACK_HOST
        self.webhook_port = DEFAULT_WEBHOOK_PORT
        self.webhook_path = DEFAULT_WEBHOOK_PATH
        self.default_webhook_events: dict = {}
        self.webhook_event_queue: list[dict] = []
        self.webhook_server: ThreadingHTTPServer | None = None

        # Model library (populated by _import_models)
        self.provider_name = "shelly"
        self.models: list[dict] = []
        self._import_models()

    @staticmethod
    def _raise_runtime_error(message: str) -> NoReturn:
        raise RuntimeError(message)

# ── BaseProvider contract ────────────────────────────────────────────────────

    def initialize_settings(self, provider_config: dict, refresh_status: bool = True) -> None:
        """Load config, build device list, optionally refresh state and install webhooks."""
        self._add_devices_from_config(provider_config)
        self.is_device_online()
        if refresh_status:
            self.refresh_all_device_statuses()
        self._set_supported_webhooks()
        self._install_webhooks()
        self._log_debug("ShellyProvider initialized successfully.")

    def get_device_status(self, device_identity: dict | int | str) -> bool:
        return self._get_device_status(device_identity)

    def refresh_all_device_statuses(self) -> None:
        for device in self._devices:
            try:
                self._get_device_status(device)
            except RuntimeError as e:
                self.logger.log_message(f"Error refreshing status for {device.get('Label')}: {e}", "error")
                raise

    def set_output(self, output_identity: dict | int | str, new_state: bool) -> tuple[bool, bool]:
        return self._change_output(output_identity, new_state)

    def get_device_location(self, device_identity: dict | int | str) -> dict | None:
        return self._get_device_location(device_identity)

    def start_services(self) -> None:
        self.webhook_server = self._start_webhook_server()

    def stop_services(self) -> None:
        if self.webhook_server:
            self.logger.log_message("Shutting down Shelly webhook server…", "debug")
            self.webhook_server.shutdown()
            self.webhook_server.server_close()
            self.webhook_server = None

    def get_normalized_status(self) -> SmartDeviceStatus:
        return SmartDeviceStatus(
            devices=[self._normalize_device(d) for d in self._devices],
            inputs=[self._normalize_component(c) for c in self._inputs],
            outputs=[self._normalize_component(c) for c in self._outputs],
            meters=[self._normalize_component(c) for c in self._meters],
            temp_probes=[self._normalize_component(c) for c in self._temp_probes],
        )

# ── Public helpers (called via SCSmartDevice delegation) ─────────────────────

    def get_device(self, device_identity: dict | int | str) -> dict:
        """Return the internal mutable device dict for the given identity.

        Raises:
            RuntimeError: If no matching device is found.
        """
        if isinstance(device_identity, dict):
            if device_identity.get("ObjectType") == "device":
                # Re-validate by ID so a dict from a different provider is rejected
                device_id = device_identity.get("ID")
                for device in self._devices:
                    if device["ID"] == device_id:
                        return device
                error_msg = f"Device with ID {device_id!r} not found in ShellyProvider."
                raise RuntimeError(error_msg)
            # Component dict — return parent device
            return self.get_device(device_identity["DeviceID"])
        for device in self._devices:
            if device["ID"] == device_identity or device["Name"] == device_identity:
                return device
        error_msg = f"Device {device_identity!r} not found."
        raise RuntimeError(error_msg)

    def get_device_component(
        self,
        component_type: str,
        component_identity: int | str,
        use_index: bool | None = None,
    ) -> dict:
        """Return the internal mutable component dict.

        Raises:
            RuntimeError: If the component type is invalid or no match is found.
        """
        lists = {
            "input": self._inputs,
            "output": self._outputs,
            "meter": self._meters,
            "temp_probe": self._temp_probes,
        }
        if component_type not in lists:
            msg = f"Invalid component type {component_type!r}."
            raise RuntimeError(msg)
        for component in lists[component_type]:
            if use_index and component["ComponentIndex"] == component_identity:
                return component
            if component["ID"] == component_identity or component["Name"] == component_identity:
                return component
        msg = f"{component_type} {component_identity!r} not found."
        raise RuntimeError(msg)

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
                        device["GetConfig"] = True
                        found_offline = True
                    self._log_debug(f"{device['Label']} is {'online' if online else 'offline'}")
        except RuntimeError as e:
            raise RuntimeError(e) from e
        return not found_offline

    def does_device_have_webhooks(self, device: dict) -> bool:
        for component in self._inputs + self._outputs + self._meters:
            if component["DeviceIndex"] != device["Index"]:
                continue
            if component.get("Webhooks"):
                return True
        return False

    def install_webhook(  # noqa: PLR0912
        self,
        event: str,
        component: dict,
        url: str | None = None,
        additional_payload: dict | None = None,
    ) -> None:
        device = self.get_device(component)
        if not any(wh.get("name") == event for wh in device.get("SupportedWebhooks", [])):
            self._raise_runtime_error(f"Event {event!r} not supported for {component.get('Name')!r}.")
        if not device.get("Online", False):
            self.logger.log_message(f"{device.get('Name')} offline — webhook {event!r} deferred.", "warning")
            device["WebhookInstallPending"] = True
            return

        if url is None:
            payload_url = (
                f"http://{self.webhook_callback_host}:{self.webhook_port}{self.webhook_path}"
                f"?Event={event}&DeviceID={device.get('ID')}"
                f"&ObjectType={component.get('ObjectType')}&ComponentID={component.get('ID')}"
            )
        else:
            payload_url = url

            def _add_arg(u: str, arg: str) -> str:
                return u + ("?" if "?" not in u else "&") + arg

            if "Event" not in url:
                payload_url = _add_arg(payload_url, f"Event={event}")
            if "DeviceID" not in url:
                payload_url = _add_arg(payload_url, f"DeviceID={device.get('ID')}")
            if "ObjectType" not in url:
                payload_url = _add_arg(payload_url, f"ObjectType={component.get('ObjectType')}")
            if "ComponentID" not in url:
                payload_url = _add_arg(payload_url, f"ComponentID={component.get('ID')}")
            if additional_payload:
                for k, v in additional_payload.items():
                    payload_url = _add_arg(payload_url, f"{k}={v}")

        try:
            err: str | None = None
            rpc_payload = {
                "id": 0,
                "method": "Webhook.Create",
                "params": {
                    "cid": component.get("ComponentIndex"),
                    "enable": True,
                    "event": event,
                    "name": f"{component.get('Name')}: {event}",
                    "urls": [str(payload_url)],
                },
            }
            result, result_data = self._rpc_request(device, rpc_payload)
            if result:
                self._log_debug(f"Installed webhook {event!r} rev {result_data.get('rev')} on {component.get('Name')!r}")
            else:
                err = f"Failed to create webhook {event!r} for {component.get('Name')!r}: {result_data}"
                self.logger.log_message(err, "error")
        except TimeoutError as e:
            msg = f"Timeout installing webhook on {device.get('Name')!r}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        except RuntimeError as e:
            msg = f"Error installing webhook on {device.get('Name')!r}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            if err:
                raise RuntimeError(err)
            self._list_installed_webhooks(device)

    def pull_webhook_event(
        self,
        device_id: int | None = None,
        device_name: str | None = None,
        component_id: int | None = None,
        component_name: str | None = None,
    ) -> dict | None:
        if device_id is None and device_name is None and component_id is None and component_name is None:
            if self.webhook_event_queue:
                return self.webhook_event_queue.pop(0)
            return None
        match_index = next(
            (
                index
                for index, event in enumerate(self.webhook_event_queue)
                if self._event_matches(event, device_id, device_name, component_id, component_name)
            ),
            None,
        )
        if match_index is None:
            return None
        return self.webhook_event_queue.pop(match_index)

    @staticmethod
    def _event_matches(
        event: dict,
        device_id: int | None,
        device_name: str | None,
        component_id: int | None,
        component_name: str | None,
    ) -> bool:
        device = event.get("Device") or {}
        component = event.get("Component") or {}
        if device_id is not None and device.get("ID") != device_id:
            return False
        if device_name is not None and device.get("Name") != device_name:
            return False
        if component_id is not None and component.get("ID") != component_id:
            return False
        return not (component_name is not None and component.get("Name") != component_name)

    def print_device_status(self, device_identity: int | str | None = None) -> str:  # noqa: PLR0912
        device_index = None
        return_str = ""
        try:
            if device_identity is not None:
                device_index = self.get_device(device_identity)["Index"]
            for idx, device in enumerate(self._devices):
                if device_index is not None and device_index != idx:
                    continue
                return_str += f"{device['Name']} (ID: {device['ID']}) — {'online' if device['Online'] else 'offline'}\n"
                return_str += f"  Model: {device['ModelName']}\n"
                return_str += f"  Simulation: {device['Simulate']}\n"
                return_str += f"  Hostname: {device['Hostname']}:{device['Port']}\n"
                return_str += f"  ExpectOffline: {device['ExpectOffline']}\n"
                return_str += f"  Generation: {device['Generation']} / Protocol: {device['Protocol']}\n"
                for ck in device.get("customkeylist", []):
                    return_str += f"  {ck}: {device[ck]}\n"
                return_str += f"  Inputs ({device['Inputs']}):\n"
                for inp in self._inputs:
                    if inp["DeviceIndex"] == idx:
                        custom = ", ".join(f"{k}: {inp[k]}" for k in inp.get("customkeylist", []))
                        return_str += f"    [{inp['ComponentIndex']}] id={inp['ID']} name={inp['Name']!r} state={inp['State']}"
                        return_str += (f", {custom}" if custom else "") + "\n"
                return_str += f"  Outputs ({device['Outputs']}):\n"
                for out in self._outputs:
                    if out["DeviceIndex"] == idx:
                        custom = ", ".join(f"{k}: {out[k]}" for k in out.get("customkeylist", []))
                        return_str += f"    [{out['ComponentIndex']}] id={out['ID']} name={out['Name']!r} state={out['State']} temp={out['Temperature']}"
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
                        return_str += f"    [{tp['ComponentIndex']}] id={tp['ID']} name={tp['Name']!r} temp={tp['Temperature']}°C  last_read={tp['LastReadingTime']}\n"
                return_str += f"  MAC: {device['MacAddress']}  Uptime: {device['Uptime']}s  Temp: {device['Temperature']}°C\n"
                return_str += f"  TotalPower: {device['TotalPower']}W  TotalEnergy: {device['TotalEnergy']}Wh\n"
                if device["SupportedWebhooks"]:
                    return_str += f"  Supported Webhooks: {len(device['SupportedWebhooks'])}\n"
                    for wh in device["SupportedWebhooks"]:
                        return_str += f"    - {wh.get('name')}\n"
                    if device["InstalledWebhooks"]:
                        return_str += f"  Installed Webhooks: {len(device['InstalledWebhooks'])}\n"
                        for wh in device["InstalledWebhooks"]:
                            return_str += f"    - {wh.get('name')}\n"
                    else:
                        return_str += "  No installed webhooks found.\n"
                else:
                    return_str += "  Webhooks not supported on this device.\n"
        except RuntimeError as e:
            raise RuntimeError(e) from e
        return return_str.strip()

    def print_model_library(self, mode_str: str = "brief", model_id: str | None = None) -> str:
        if not self.models:
            return "No models loaded."
        lines = ["Shelly Model Library:"]
        for model in self.models:
            if model_id is not None and model["model"] != model_id:
                continue
            if mode_str == "brief":
                lines.append(f"  {model['model']} — {model['name']}")
            else:
                lines.append(
                    f"  {model['model']} — {model['name']} gen{model.get('generation')} "
                    f"in={model.get('inputs')} out={model.get('outputs')} meters={model.get('meters')}"
                )
        return "\n".join(lines)

    def get_device_information(self, device_identity: dict | int | str, refresh_status: bool = False) -> dict:
        try:
            device = self.get_device(device_identity)
            if refresh_status:
                self._get_device_status(device)
        except RuntimeError as e:
            raise RuntimeError(e) from e
        idx = device["Index"]
        info = device.copy()
        info["Inputs"] = [c for c in self._inputs if c["DeviceIndex"] == idx]
        info["Outputs"] = [c for c in self._outputs if c["DeviceIndex"] == idx]
        info["Meters"] = [c for c in self._meters if c["DeviceIndex"] == idx]
        info["TempProbes"] = [c for c in self._temp_probes if c["DeviceIndex"] == idx]
        return info

# ── Internal — config loading ────────────────────────────────────────────────

    def _add_devices_from_config(self, settings: dict) -> None:
        self.allow_debug_logging = settings.get("AllowDebugLogging", False)
        self.response_timeout = settings.get("ResponseTimeout", self.response_timeout)
        self.retry_count = settings.get("RetryCount", self.retry_count)
        self.retry_delay = settings.get("RetryDelay", self.retry_delay)
        self.ping_allowed = settings.get("PingAllowed", True)

        relative_folder = settings.get("SimulationFileFolder", "simulation_files")
        self.simulation_file_folder = SCCommon.select_folder_location(relative_folder, create_folder=True)

        webhook_cfg: dict = settings.get("ShellyWebhooks") or {}
        self.webhook_enabled = bool(webhook_cfg.get("Enabled")) and self._app_wake_event is not None
        self.webhook_callback_host = webhook_cfg.get("Host", DEFAULT_WEBHOOK_CALLBACK_HOST)
        self.webhook_port = int(webhook_cfg.get("Port", DEFAULT_WEBHOOK_PORT))
        self.webhook_path = webhook_cfg.get("Path", DEFAULT_WEBHOOK_PATH)
        self.default_webhook_events = webhook_cfg.get("DefaultWebhooks") or {}

        self._devices.clear()
        self._inputs.clear()
        self._outputs.clear()
        self._meters.clear()
        self._temp_probes.clear()

        for device_cfg in settings.get("Devices", []):
            if str(device_cfg.get("Model")) == "Tasmota":
                continue  # Tasmota devices are handled by TasmotaProvider
            self._add_device(device_cfg)

    def _add_device(self, device_config: dict) -> None:
        device_index = len(self._devices)
        new_device = self._get_device_attributes(str(device_config.get("Model")))

        new_device["Index"] = device_index
        client_name = device_config.get("Name")
        device_id = device_config.get("ID", device_index + 1)
        new_device["ID"] = device_id
        new_device["Name"] = client_name or f"Device {device_id}"
        new_device["ClientName"] = new_device["Name"]  # internal alias kept for compat
        new_device["Label"] = f"{new_device['Name']} (ID: {device_id})"
        new_device["Simulate"] = bool(device_config.get("Simulate"))
        new_device["ExpectOffline"] = bool(device_config.get("ExpectOffline"))
        new_device["Hostname"] = device_config.get("Hostname")
        new_device["Port"] = device_config.get("Port", 80)
        new_device["TempProbes"] = len(device_config.get("TempProbes") or [])

        if not new_device["Simulate"] and not new_device["Hostname"]:
            self._raise_runtime_error(f"Device {new_device['Label']} has no Hostname configured.")
        if new_device["Hostname"] and not SCCommon.is_valid_hostname(new_device["Hostname"]):
            self._raise_runtime_error(
                f"Device {new_device['Label']} has invalid hostname {new_device['Hostname']!r}."
            )

        for existing in self._devices:
            if existing["Name"] == new_device["Name"]:
                self._raise_runtime_error(f"Device Name {new_device['Name']!r} must be unique.")
            if existing["ID"] == new_device["ID"]:
                self._raise_runtime_error(f"Device ID {new_device['ID']} must be unique.")

        if not new_device["ID"] and not new_device["Name"]:
            self._raise_runtime_error("Device must have an ID or a Name.")

        if new_device["Simulate"]:
            fname = "".join(c if c.isalnum() else "_" for c in new_device["Name"]) + ".json"
            new_device["SimulationFile"] = self.simulation_file_folder / fname  # pyright: ignore[reportOptionalOperand]

        new_device["customkeylist"] = []
        for key, value in device_config.items():
            if key not in new_device:
                new_device[key] = value
                new_device["customkeylist"].append(key)

        self._devices.append(new_device)

        self._add_device_components(device_index, "input", device_config.get("Inputs"))
        self._add_device_components(device_index, "output", device_config.get("Outputs"))
        self._add_device_components(device_index, "meter", device_config.get("Meters"))
        self._add_device_components(device_index, "temp_probe", device_config.get("TempProbes"))

        self._import_device_information_from_json(new_device, create_if_no_file=True)
        self._log_debug(f"Added Shelly device {new_device['Label']}.")

    def _get_device_attributes(self, device_model: str) -> dict:
        if not self.models:
            self._raise_runtime_error(f"Model file {SHELLY_MODEL_FILE} not loaded.")
        model_dict = next((m for m in self.models if m.get("model") == device_model), None)
        if model_dict is None:
            self._raise_runtime_error(f"Device model {device_model!r} not found in {SHELLY_MODEL_FILE}.")
        device: dict = {
            "Index": 0,
            "Model": device_model,
            "Name": None,
            "ClientName": None,
            "ID": None,
            "ObjectType": "device",
            "Simulate": False,
            "GetConfig": True,
            "SimulationFile": None,
            "ExpectOffline": False,
            "ModelName": model_dict.get("name", "Unknown"),
            "Label": None,
            "URL": model_dict.get("url"),
            "Hostname": None,
            "Port": 80,
            "Generation": model_dict.get("generation", 3),
            "Protocol": model_dict.get("protocol", "RPC"),
            "Inputs": model_dict.get("inputs", 1),
            "Outputs": model_dict.get("outputs", 1),
            "Meters": model_dict.get("meters", 0),
            "TempProbes": 0,
            "MetersSeperate": model_dict.get("meters_seperate", False),
            "TemperatureMonitoring": model_dict.get("temperature_monitoring", True),
            "Online": False,
            "MacAddress": None,
            "Temperature": None,
            "Uptime": None,
            "RestartRequired": None,
            "WebhookInstallPending": False,
            "SupportedWebhooks": [],
            "InstalledWebhooks": [],
            "customkeylist": [],
            "TotalPower": 0.0,
            "TotalEnergy": 0.0,
        }
        if not device["MetersSeperate"] and device["Meters"] > 0 and device["Meters"] != device["Outputs"]:
            self._raise_runtime_error(
                f"Model {device_model}: meters ({device['Meters']}) ≠ outputs ({device['Outputs']}) "
                "when meters are not separate."
            )
        return device

    def _add_device_components(  # noqa: PLR0912
        self,
        device_index: int,
        component_type: str,
        component_config: list[dict] | None,
    ) -> None:
        if device_index < 0 or device_index >= len(self._devices):
            self._raise_runtime_error(f"Invalid device index {device_index}.")

        type_map = {
            "input": ("Inputs", self._inputs, "Input"),
            "output": ("Outputs", self._outputs, "Output"),
            "meter": ("Meters", self._meters, "Meter"),
            "temp_probe": ("TempProbes", self._temp_probes, "TempProbe"),
        }
        if component_type not in type_map:
            self._raise_runtime_error(f"Invalid component type {component_type!r}.")

        count_key, storage, prefix = type_map[component_type]
        device = self._devices[device_index]
        expected = device[count_key]

        if component_config is not None and (
            not isinstance(component_config, list) or len(component_config) != expected
        ):
            self._raise_runtime_error(
                f"Device {device['Label']}: expected {expected} {component_type}(s), "
                f"got {len(component_config) if isinstance(component_config, list) else 'non-list'}."
            )

        for comp_idx in range(expected):
            new_comp = self._new_device_component(device_index, component_type)

            if component_config is None:
                new_comp["ID"] = len(storage) + 1
                new_comp["Name"] = f"{prefix} {new_comp['ID']}"
                new_comp["Webhooks"] = False
            else:
                cfg = component_config[comp_idx]
                new_comp["ID"] = cfg.get("ID", len(storage) + 1)
                new_comp["Name"] = cfg.get("Name", f"{prefix} {new_comp['ID']}")
                new_comp["Webhooks"] = cfg.get("Webhooks", False)

            new_comp["ComponentIndex"] = comp_idx

            if component_type == "input":
                new_comp["State"] = False
            elif component_type == "output":
                new_comp["State"] = False
                new_comp["HasMeter"] = not device["MetersSeperate"]
            elif component_type == "meter":
                new_comp["OnOutput"] = not device["MetersSeperate"]
                new_comp["MockRate"] = (component_config[comp_idx].get("MockRate", 0) if component_config else 0)
            elif component_type == "temp_probe":
                new_comp["RequiresOutput"] = (component_config[comp_idx].get("RequiresOutput") if component_config else None)
                if new_comp["Name"] == device["Name"]:
                    new_comp["ProbeID"] = -1

            new_comp["customkeylist"] = []
            if component_config:
                for key, value in component_config[comp_idx].items():
                    if key not in new_comp:
                        new_comp[key] = value
                        new_comp["customkeylist"].append(key)

            for existing in storage:
                if existing["Name"] == new_comp["Name"]:
                    self._raise_runtime_error(f"{prefix} Name {new_comp['Name']!r} must be unique.")
                if existing["ID"] == new_comp["ID"]:
                    self._raise_runtime_error(f"{prefix} ID {new_comp['ID']} must be unique.")

            if not new_comp["ID"] and not new_comp["Name"]:
                self._raise_runtime_error(f"{prefix} must have an ID or a Name.")

            storage.append(new_comp)

    def _new_device_component(self, device_index: int, component_type: str) -> dict:
        device = self._devices[device_index]
        comp: dict = {
            "DeviceIndex": device_index,
            "DeviceID": device["ID"],
            "ComponentIndex": None,
            "ObjectType": component_type,
            "ID": None,
            "Name": None,
            "Webhooks": False,
            "customkeylist": [],
        }
        if component_type == "input":
            comp["State"] = False
        elif component_type == "output":
            comp["HasMeter"] = not device["MetersSeperate"]
            comp["State"] = False
            comp["Temperature"] = None
        elif component_type == "meter":
            comp["OnOutput"] = not device["MetersSeperate"]
            comp["Power"] = None
            comp["Voltage"] = None
            comp["Current"] = None
            comp["PowerFactor"] = None
            comp["Energy"] = None
            comp["MockRate"] = 0
        elif component_type == "temp_probe":
            comp["ProbeID"] = None
            comp["Temperature"] = None
            comp["LastReadingTime"] = None
            comp["RequiresOutput"] = None
        return comp

# ── Internal — device status ─────────────────────────────────────────────────

    def _get_device_status(self, device_identity: dict | int | str) -> bool:  # noqa: PLR0912, PLR0914, PLR0915
        device = self.get_device(device_identity)

        if device["Simulate"]:
            self._import_device_information_from_json(device, create_if_no_file=True)
            return True

        self._process_device_config(device)

        try:
            em_result_data: list[dict] = []
            emdata_result_data: list[dict] = []
            if device["Protocol"] == "RPC":
                result, result_data = self._rpc_request(device, {"id": 0, "method": "Shelly.GetStatus"})
                if device["MetersSeperate"]:
                    for meter_idx in range(device["Meters"]):
                        ok, md = self._rpc_request(device, {"id": 0, "method": "EM1.GetStatus", "params": {"id": meter_idx}})
                        if ok:
                            em_result_data.append(md)
                        ok, md = self._rpc_request(device, {"id": 0, "method": "EM1Data.GetStatus", "params": {"id": meter_idx}})
                        if ok:
                            emdata_result_data.append(md)
            elif device["Protocol"] == "REST":
                result, result_data = self._rest_request(device, "status")
                if not device["MetersSeperate"]:
                    self._raise_runtime_error(
                        f"Model {device['Model']} has combined meters & switches — not supported."
                    )
            else:
                self._raise_runtime_error(f"Unsupported protocol {device['Protocol']!r} for {device['Label']}.")
        except TimeoutError as e:
            self.logger.log_message(f"Timeout getting status for {device['Label']}: {e}", "error")
            raise
        except RuntimeError as e:
            self.logger.log_message(f"Error getting status for {device['Label']}: {e}", "error")
            raise

        if not result:
            self._set_device_outputs_off(device)
            return False

        try:  # noqa: PLR1702
            if device["Protocol"] == "RPC":
                device["MacAddress"] = result_data.get("sys", {}).get("mac")
                device["Uptime"] = result_data.get("sys", {}).get("uptime")
                device["RestartRequired"] = result_data.get("sys", {}).get("restart_required", False)

                for inp in self._inputs:
                    if inp["DeviceIndex"] == device["Index"]:
                        inp["State"] = result_data.get(f"input:{inp['ComponentIndex']}", {}).get("state", False)

                for out in self._outputs:
                    if out["DeviceIndex"] == device["Index"]:
                        ci = out["ComponentIndex"]
                        out["State"] = result_data.get(f"switch:{ci}", {}).get("output", False)
                        if device["TemperatureMonitoring"]:
                            out["Temperature"] = result_data.get(f"switch:{ci}", {}).get("temperature", {}).get("tC")

                for meter in self._meters:
                    if meter["DeviceIndex"] == device["Index"]:
                        ci = meter["ComponentIndex"]
                        if device["MetersSeperate"]:
                            if len(em_result_data) != device["Meters"] or len(emdata_result_data) != device["Meters"]:
                                self._raise_runtime_error(f"{device['Label']}: EM1.GetStatus call count mismatch.")
                            meter["Power"] = abs(em_result_data[ci].get("act_power") or 0) or None
                            meter["Voltage"] = em_result_data[ci].get("voltage")
                            meter["Current"] = em_result_data[ci].get("current")
                            meter["PowerFactor"] = em_result_data[ci].get("pf")
                            meter["Energy"] = emdata_result_data[ci].get("total_act_energy")
                        else:
                            sw = result_data.get(f"switch:{ci}", {})
                            raw_p = sw.get("apower")
                            meter["Power"] = abs(raw_p) if raw_p is not None else None
                            meter["Voltage"] = sw.get("voltage")
                            meter["Current"] = sw.get("current")
                            meter["PowerFactor"] = sw.get("pf")
                            meter["Energy"] = sw.get("aenergy", {}).get("total")

                self._calculate_gen2_device_temp(device)

                for tp in self._temp_probes:
                    if tp["DeviceIndex"] == device["Index"]:
                        read_tp = True
                        req_out_name = tp.get("RequiresOutput")
                        if req_out_name:
                            req_out = next((o for o in self._outputs if o["Name"] == req_out_name), None)
                            if req_out and not req_out["State"]:
                                read_tp = False
                        if read_tp:
                            probe_id = tp["ProbeID"]
                            if probe_id == -1:
                                tp["Temperature"] = device["Temperature"]
                            elif probe_id is not None:
                                tp["Temperature"] = result_data.get(f"temperature:{probe_id}", {}).get("tC")
                            tp["LastReadingTime"] = DateHelper.now()

            else:  # REST (gen 1)
                device["MacAddress"] = result_data.get("mac")
                device["Uptime"] = result_data.get("uptime")
                device["RestartRequired"] = result_data.get("update", {}).get("has_update", False)
                if device["TemperatureMonitoring"]:
                    device["Temperature"] = result_data.get("temperature")

                for inp in self._inputs:
                    if inp["DeviceIndex"] == device["Index"]:
                        ci = inp["ComponentIndex"]
                        inp["State"] = (result_data.get("inputs") or [])[ci].get("input", False) if result_data.get("inputs") else False

                for out in self._outputs:
                    if out["DeviceIndex"] == device["Index"]:
                        ci = out["ComponentIndex"]
                        relays = result_data.get("relays") or []
                        out["State"] = relays[ci].get("ison", False) if ci < len(relays) else False
                        if device["TemperatureMonitoring"]:
                            out["Temperature"] = device["Temperature"]

                for meter in self._meters:
                    if meter["DeviceIndex"] == device["Index"]:
                        ci = meter["ComponentIndex"]
                        meter_key = "emeters" if result_data.get("emeters") else "meters"
                        ml = result_data.get(meter_key) or []
                        md = ml[ci] if ci < len(ml) else {}
                        raw_p = md.get("power")
                        meter["Power"] = abs(raw_p) if raw_p is not None else None
                        meter["Voltage"] = md.get("voltage")
                        meter["Energy"] = md.get("total")
                        meter["Current"] = None
                        meter["PowerFactor"] = None

        except (AttributeError, KeyError, RuntimeError) as e:
            msg = f"Error parsing status for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e

        self._calculate_device_energy_totals(device)

        if device["Online"] and device["WebhookInstallPending"]:
            self._set_supported_webhooks(device)
            if self.does_device_have_webhooks(device) and not device["InstalledWebhooks"]:
                self._install_webhooks(device)

        self._log_debug(f"{device['Label']} status retrieved.")
        return True

    def _change_output(self, output_identity: dict | int | str, new_state: bool) -> tuple[bool, bool]:
        if isinstance(output_identity, dict):
            device_output = self.get_device_component("output", output_identity["ID"])
        else:
            device_output = self.get_device_component("output", output_identity)

        device = self._devices[device_output["DeviceIndex"]]
        current_state = device_output["State"]

        if not device["Simulate"]:
            try:
                if not self._get_device_status(device):
                    if not device.get("ExpectOffline"):
                        self.logger.log_message(f"{device['Label']} offline — cannot change output.", "warning")
                    return False, False

                if device["Protocol"] == "RPC":
                    result, _ = self._rpc_request(device, {
                        "id": 0,
                        "method": "Switch.Set",
                        "params": {"id": device_output["ComponentIndex"], "on": new_state},
                    })
                elif device["Protocol"] == "REST":
                    result, _ = self._rest_request(
                        device, f"relay/{device_output['ComponentIndex']}?turn={'on' if new_state else 'off'}"
                    )
                else:
                    self._raise_runtime_error(f"Unsupported protocol {device['Protocol']!r}.")
            except TimeoutError as e:
                self.logger.log_message(f"Timeout changing output on {device['Label']}: {e}", "error")
                raise
            except RuntimeError as e:
                self.logger.log_message(f"Error changing output on {device['Label']}: {e}", "error")
                raise

            if not result:
                return False, False

        device_output["State"] = new_state
        if device["Simulate"]:
            self._export_device_information_to_json(device)

        did_change = new_state != current_state
        self._log_debug(
            f"Output {output_identity!r} on {device['Label']} "
            f"{'changed to' if did_change else 'already'} {'on' if new_state else 'off'}."
        )
        return True, did_change

    def _get_device_location(self, device_identity: dict | int | str) -> dict | None:
        device = self.get_device(device_identity)

        if device["Simulate"]:
            return {"tz": "Australia/Sydney", "lat": -33.8688, "lon": 151.2093}
        if not device["Online"] or device["Protocol"] != "RPC":
            return None

        try:
            result, result_data = self._rpc_request(device, {"id": 0, "method": "Shelly.DetectLocation"})
            if not result:
                self.logger.log_message(f"DetectLocation failed for {device.get('Name')}: {result_data}", "error")
                return None
        except (TimeoutError, RuntimeError) as e:
            self.logger.log_message(f"Error getting location for {device['Label']}: {e}", "error")
            raise
        else:
            return result_data

# ── Internal — webhook management ────────────────────────────────────────────

    def _set_supported_webhooks(self, selected_device: dict | None = None) -> None:
        for device in self._devices:
            if selected_device and device["Index"] != selected_device["Index"]:
                continue
            if device.get("Protocol") != "RPC" or device.get("Simulate") or not device.get("Online"):
                if not device.get("Simulate") and not device.get("Online"):
                    device["WebhookInstallPending"] = True
                continue
            try:
                result, result_data = self._rpc_request(device, {"id": 0, "method": "Webhook.ListSupported"})
                if not result:
                    self.logger.log_message(f"Webhook.ListSupported failed for {device.get('Name')!r}.", "error")
                    continue
            except TimeoutError as e:
                msg = f"Timeout listing supported webhooks for {device.get('Name')!r}: {e}"
                self.logger.log_message(msg, "error")
                raise RuntimeError(msg) from e
            except RuntimeError as e:
                msg = f"Error listing supported webhooks for {device.get('Name')!r}: {e}"
                self.logger.log_message(msg, "error")
                raise RuntimeError(msg) from e
            else:
                types_dict = result_data.get("types", {})
                device["SupportedWebhooks"] = [
                    {"name": k, **({"attrs": v["attrs"]} if "attrs" in v else {})}
                    for k, v in types_dict.items()
                ]

    def _install_webhooks(self, selected_device: dict | None = None) -> None:
        if not self.webhook_enabled:
            return
        for device in self._devices:
            if selected_device and device["Index"] != selected_device["Index"]:
                continue
            if not device["SupportedWebhooks"] or not device.get("Online"):
                if not device.get("Online"):
                    device["WebhookInstallPending"] = True
                continue
            result, result_data = self._rpc_request(device, {"id": 0, "method": "Webhook.DeleteAll"})
            if not result:
                self.logger.log_message(f"Failed to delete existing webhooks for {device.get('Name')}: {result_data}", "error")
                continue
            for component in self._inputs + self._outputs + self._meters:
                if component["DeviceIndex"] != device["Index"] or not component.get("Webhooks"):
                    continue
                for event in self._get_default_webhook_events(component):
                    try:
                        self.install_webhook(event, component)
                    except RuntimeError as e:
                        msg = f"Error installing webhook {event!r} on {component.get('Name')!r}: {e}"
                        self.logger.log_message(msg, "error")
                        raise RuntimeError(msg) from e

    def _get_default_webhook_events(self, component: dict) -> list[str]:
        obj_type = component.get("ObjectType")
        if obj_type == "input":
            return self.default_webhook_events.get("Inputs") or ["input.toggle_on", "input.toggle_off"]
        if obj_type == "output":
            return self.default_webhook_events.get("Outputs") or ["switch.on", "switch.off"]
        if obj_type == "meter":
            return self.default_webhook_events.get("Meters") or []
        return []

    def _list_installed_webhooks(self, selected_device: dict | None = None) -> None:
        for device in self._devices:
            if not device["SupportedWebhooks"]:
                continue
            if selected_device is not None and device != selected_device:
                continue
            try:
                result, result_data = self._rpc_request(device, {"id": 0, "method": "Webhook.List"})
                if result:
                    device["InstalledWebhooks"] = result_data.get("hooks", [])
            except (TimeoutError, RuntimeError) as e:
                self.logger.log_message(f"Error listing installed webhooks for {device.get('Name')!r}: {e}", "error")

    def _start_webhook_server(self) -> ThreadingHTTPServer | None:
        if not self.webhook_enabled:
            self._log_debug("Webhook server disabled.")
            return None
        if self._app_wake_event is None:
            self._raise_runtime_error("app_wake_event required to start the webhook server.")
        try:
            server = ThreadingHTTPServer((DEFAULT_WEBHOOK_LISTEN, self.webhook_port), _ShellyWebhookHandler)  # pyright: ignore[reportArgumentType]
            server.app_wake_event = self._app_wake_event  # type: ignore[attr-defined]
            server.controller = self  # type: ignore[attr-defined]
            server.webhook_path = self.webhook_path  # type: ignore[attr-defined]
            server.logger = self.logger  # type: ignore[attr-defined]
            t = threading.Thread(target=server.serve_forever, daemon=True, name="ShellyWebhookServer")
            t.start()
        except (RuntimeError, OSError) as e:
            msg = f"Failed to start webhook server: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            self._log_debug(f"Webhook server listening on {DEFAULT_WEBHOOK_LISTEN}:{self.webhook_port}{self.webhook_path}")
            return server

    def _push_webhook_event(self, args: dict) -> None:
        self._log_debug(f"Webhook event received: {args}")
        event_entry: dict = {"timestamp": DateHelper.now()}
        for k, v in args.items():
            event_entry[k] = v[0] if isinstance(v, list) and len(v) == 1 else v  # pyright: ignore[reportArgumentType]

        if event_entry.get("DeviceID") is not None:
            try:
                device = self.get_device(int(event_entry["DeviceID"]))
                event_entry.pop("DeviceID", None)
                event_entry["Device"] = device
            except RuntimeError:
                pass

        if (
            event_entry.get("ObjectType") is not None
            and event_entry.get("ComponentID") is not None
            and event_entry.get("Device") is not None
        ):
            try:
                component = self.get_device_component(
                    event_entry["ObjectType"], int(event_entry["ComponentID"])
                )
                event_entry.pop("ObjectType", None)
                event_entry.pop("ComponentID", None)
                event_entry["Component"] = component
            except RuntimeError:
                pass

        self.webhook_event_queue.append(event_entry)

# ── Internal — HTTP requests ─────────────────────────────────────────────────

    def _rest_request(self, device: dict, url_args: str) -> tuple[bool, dict]:
        self._log_debug(f"REST {device['Label']} → {url_args}")
        if not self.is_device_online(device["ID"]):
            if not device.get("ExpectOffline"):
                self.logger.log_message(f"{device['Label']} offline — REST request skipped.", "warning")
            return False, {}

        url = f"http://{device['Hostname']}:{device['Port']}/{url_args}"
        headers = {"Content-Type": "application/json"}
        retry_count = 0
        fatal: str | None = None
        while retry_count <= self.retry_count and fatal is None:
            try:
                response = requests.get(url, headers=headers, timeout=self.response_timeout)
                response.raise_for_status()
                if response.status_code != 200:
                    fatal = f"REST {device['Label']} returned {response.status_code}."
                    raise RuntimeError(fatal)
                data = response.json()
                if not data:
                    fatal = f"REST {device['Label']} returned empty payload."
                    raise RuntimeError(fatal)
            except requests.exceptions.Timeout as e:
                retry_count += 1
                if retry_count > self.retry_count:
                    fatal = f"REST timeout for {device['Label']} after {self.retry_count} retries."
                    raise TimeoutError(fatal) from e
            except requests.exceptions.ConnectionError as e:
                fatal = f"REST connection error for {device['Label']}: {e}"
                raise RuntimeError(fatal) from e
            except requests.exceptions.RequestException as e:
                fatal = f"REST request error for {device['Label']}: {e}"
                raise RuntimeError(fatal) from e
            else:
                return True, data
            if fatal is None:
                self._log_debug(f"REST retry {retry_count} for {device['Label']}")
                time.sleep(self.retry_delay)
        return False, {}

    def _rpc_request(self, device: dict, payload: dict) -> tuple[bool, dict]:
        self._log_debug(f"RPC {device['Label']} → {payload.get('method')}")
        if not self.is_device_online(device):
            if not device.get("ExpectOffline"):
                self.logger.log_message(f"{device['Label']} offline — RPC request skipped.", "warning")
            return False, {}

        url = f"http://{device['Hostname']}:{device['Port']}/rpc"
        headers = {"Content-Type": "application/json"}
        retry_count = 0
        fatal: str | None = None
        while retry_count <= self.retry_count and fatal is None:
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=self.response_timeout)
                response.raise_for_status()
                if response.status_code == 401:
                    fatal = f"RPC 401 Unauthorized for {device['Label']}."
                    raise RuntimeError(fatal)
                if response.status_code != 200:
                    fatal = f"RPC {device['Label']} returned {response.status_code}."
                    raise RuntimeError(fatal)
                resp_payload = response.json()
                result_data = resp_payload.get("result")
                if not result_data:
                    shelly_err = resp_payload.get("error", {})
                    fatal = (
                        f"RPC {device['Label']} error: {shelly_err.get('message')} "
                        f"(code {shelly_err.get('code')})"
                        if shelly_err
                        else f"RPC {device['Label']} returned empty result."
                    )
                    raise RuntimeError(fatal)
            except requests.exceptions.Timeout as e:
                retry_count += 1
                if retry_count > self.retry_count:
                    fatal = f"RPC timeout for {device['Label']} after {self.retry_count} retries."
                    raise TimeoutError(fatal) from e
            except requests.exceptions.ConnectionError as e:
                fatal = f"RPC connection error for {device['Label']}: {e}"
                raise RuntimeError(fatal) from e
            except requests.exceptions.RequestException as e:
                fatal = f"RPC request error for {device['Label']}: {e}"
                raise RuntimeError(fatal) from e
            else:
                if self.allow_debug_logging:
                    debug_file = Path(f"{device['Name']} RPC {payload.get('method')} response.json")
                    try:
                        with debug_file.open("w", encoding="utf-8") as f:
                            json.dump(resp_payload, f, indent=2, ensure_ascii=False)
                    except OSError:
                        pass
                return True, result_data
            if fatal is None:
                self._log_debug(f"RPC retry {retry_count} for {device['Label']}")
                time.sleep(self.retry_delay)
        return False, {}

# ── Internal — device config / temp probes ───────────────────────────────────

    def _process_device_config(self, device: dict) -> None:
        if device["Simulate"] or not device.get("GetConfig") or not self.is_device_online(device):
            return
        try:
            config_response = self._get_device_config(device)
        except (TimeoutError, RuntimeError):
            return
        device["GetConfig"] = False
        if config_response and device["Protocol"] == "RPC":
            self._extract_temp_probe_config(device, config_response)

    def _get_device_config(self, device: dict) -> dict:
        if device["Simulate"]:
            return {}
        try:
            if device["Protocol"] == "RPC":
                result, result_data = self._rpc_request(device, {"id": 0, "method": "Shelly.GetConfig"})
            else:
                result, result_data = self._rest_request(device, "settings")
        except (TimeoutError, RuntimeError) as e:
            self.logger.log_message(f"Error getting config for {device['Label']}: {e}", "error")
            raise
        return result_data if result else {}

    def _extract_temp_probe_config(self, device: dict, payload: dict) -> None:
        probe_id = FIRST_TEMP_PROBE_ID
        while True:
            probe_data = payload.get(f"temperature:{probe_id}", {})
            if not probe_data:
                break
            probe_id = probe_data.get("id", probe_id)
            probe_name = probe_data.get("name")
            if probe_name:
                for tp in self._temp_probes:
                    if tp["DeviceIndex"] == device["Index"] and tp.get("Name") == probe_name:
                        tp["ProbeID"] = probe_id
            probe_id += 1
            if (probe_id - FIRST_TEMP_PROBE_ID) > 20:
                break

# ── Internal — calculations / helpers ────────────────────────────────────────

    def _calculate_device_energy_totals(self, device: dict) -> None:
        if device["Meters"] > 0:
            total_p = sum(m["Power"] or 0 for m in self._meters if m["DeviceIndex"] == device["Index"])
            total_e = sum(m["Energy"] or 0 for m in self._meters if m["DeviceIndex"] == device["Index"])
            device["TotalPower"] = total_p
            device["TotalEnergy"] = total_e

    def _calculate_gen2_device_temp(self, device: dict) -> None:
        if device["TemperatureMonitoring"] and device["Outputs"] > 0:
            temps = [o["Temperature"] for o in self._outputs if o["DeviceIndex"] == device["Index"] and o.get("Temperature")]
            if temps:
                device["Temperature"] = sum(temps) / len(temps)

    def _set_device_outputs_off(self, device: dict) -> None:
        for out in self._outputs:
            if out["DeviceIndex"] == device["Index"]:
                out["State"] = False

    def _import_models(self) -> None:
        try:
            pkg_files = resources.files("sc_smart_device")
            model_file = pkg_files / SHELLY_MODEL_FILE
            with model_file.open("r", encoding="utf-8") as f:
                self.models = json.load(f)
        except FileNotFoundError as e:
            msg = f"Could not find {SHELLY_MODEL_FILE} in package."
            raise RuntimeError(msg) from e
        except json.JSONDecodeError as e:
            msg = f"JSON error loading {SHELLY_MODEL_FILE}: {e}"
            raise RuntimeError(msg) from e

    @staticmethod
    def get_model_component_counts(model_name: str) -> dict[str, int] | None:
        """Return ``{"inputs": N, "outputs": N, "meters": N}`` for a Shelly model, or None.

        Used by the global ID pre-processor in SCSmartDevice so that model-driven
        components (omitted from the YAML config) can still receive globally unique IDs.
        """
        try:
            pkg_files = resources.files("sc_smart_device")
            model_file = pkg_files / SHELLY_MODEL_FILE
            with model_file.open("r", encoding="utf-8") as f:
                models = json.load(f)
            model = next((m for m in models if m.get("model") == model_name), None)
            if model:
                return {
                    "inputs": int(model.get("inputs", 0)),
                    "outputs": int(model.get("outputs", 0)),
                    "meters": int(model.get("meters", 0)),
                }
        except (FileNotFoundError, json.JSONDecodeError, StopIteration):
            pass
        return None

    def _log_debug(self, message: str) -> None:
        if self.allow_debug_logging:
            self.logger.log_message(message, "debug")

# ── Internal — simulation file I/O ───────────────────────────────────────────

    @staticmethod
    def _get_simulation_file_path(device: dict) -> Path:
        file_path = device["SimulationFile"]
        if not isinstance(file_path, Path):
            msg = f"No simulation file path for {device['Label']}."
            raise RuntimeError(msg)  # noqa: TRY004
        return file_path

    def _merge_simulated_inputs(self, device_index: int, inputs: list[dict]) -> None:
        for imported_input in inputs:
            for input_component in self._inputs:
                if (
                    input_component["DeviceIndex"] == device_index
                    and input_component["ComponentIndex"] == imported_input.get("ComponentIndex")
                ):
                    input_component["State"] = imported_input.get("State", input_component["State"])

    def _merge_simulated_outputs(self, device: dict, device_index: int, outputs: list[dict]) -> None:
        for imported_output in outputs:
            for output_component in self._outputs:
                if (
                    output_component["DeviceIndex"] == device_index
                    and output_component["ComponentIndex"] == imported_output.get("ComponentIndex")
                ):
                    output_component["State"] = imported_output.get("State", output_component["State"])
                    if device["TemperatureMonitoring"]:
                        output_component["Temperature"] = imported_output.get(
                            "Temperature", output_component.get("Temperature")
                        )

    def _merge_simulated_meters(self, device_index: int, meters: list[dict]) -> None:
        for imported_meter in meters:
            for meter in self._meters:
                if meter["DeviceIndex"] != device_index or meter["ComponentIndex"] != imported_meter.get("ComponentIndex"):
                    continue
                for key in ("Power", "Voltage", "Current", "PowerFactor", "Energy"):
                    if key in imported_meter:
                        meter[key] = imported_meter[key]
                if meter.get("MockRate", 0) > 0:
                    local_tz = DateHelper.get_local_timezone()
                    elapsed = (DateHelper.now() - dt.datetime(2025, 1, 1, tzinfo=local_tz)).total_seconds()
                    meter["Energy"] = meter["MockRate"] * elapsed

    def _merge_simulated_temp_probes(self, device_index: int, temp_probes: list[dict]) -> None:
        for imported_probe in temp_probes:
            for temp_probe in self._temp_probes:
                if (
                    temp_probe["DeviceIndex"] == device_index
                    and temp_probe["ComponentIndex"] == imported_probe.get("ComponentIndex")
                ):
                    temp_probe["Temperature"] = imported_probe.get("Temperature", temp_probe.get("Temperature"))
                    temp_probe["LastReadingTime"] = DateHelper.now()

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
            msg = f"Error exporting simulation file for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            self._log_debug(f"Simulation file written: {file_path}")
            return True

    def _import_device_information_from_json(self, device: dict, create_if_no_file: bool) -> bool:
        if not device.get("Simulate"):
            return False
        file_path = self._get_simulation_file_path(device)
        if not file_path.exists():
            if create_if_no_file:
                return self._export_device_information_to_json(device)
            msg = f"Simulation file {file_path} not found."
            raise RuntimeError(msg)
        try:
            with file_path.open("r", encoding="utf-8") as f:
                device_info = json.load(f)
            device_index = device["Index"]
            for key in ("MACAddress", "Uptime", "RestartRequired"):
                if key in device_info and key in device:
                    device[key] = device_info[key]
            if device_info.get("Inputs") and device["Inputs"] > 0:
                self._merge_simulated_inputs(device_index, device_info["Inputs"])
            if device_info.get("Outputs") and device["Outputs"] > 0:
                self._merge_simulated_outputs(device, device_index, device_info["Outputs"])
            if device_info.get("Meters") and device["Meters"] > 0:
                self._merge_simulated_meters(device_index, device_info["Meters"])
            if device_info.get("TempProbes") and device["TempProbes"] > 0:
                self._merge_simulated_temp_probes(device_index, device_info["TempProbes"])
            self._calculate_device_energy_totals(device)
            self._calculate_gen2_device_temp(device)
        except (OSError, json.JSONDecodeError, KeyError, RuntimeError) as e:
            msg = f"Error importing simulation file for {device['Label']}: {e}"
            self.logger.log_message(msg, "error")
            raise RuntimeError(msg) from e
        else:
            self._log_debug(f"Simulation data loaded from {file_path}")
            return True

# ── Normalized snapshot helpers ──────────────────────────────────────────────

    @staticmethod
    def _normalize_device(device: dict) -> dict:
        caps: set[DeviceCapability] = {DeviceCapability.POLLING}
        if device.get("Outputs", 0) > 0:
            caps.add(DeviceCapability.OUTPUT_CONTROL)
            caps.add(DeviceCapability.OUTPUT_STATE)
        if device.get("Inputs", 0) > 0:
            caps.add(DeviceCapability.INPUT_READ)
        if device.get("TemperatureMonitoring"):
            caps.add(DeviceCapability.TEMPERATURE)
        if device.get("Meters", 0) > 0:
            caps.add(DeviceCapability.METERING)
        if device.get("Simulate"):
            caps.add(DeviceCapability.SIMULATION)
        if device.get("Protocol") == "RPC":
            caps.add(DeviceCapability.WEBHOOKS)
            caps.add(DeviceCapability.DEVICE_LOCATION)
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
        internal = {"customkeylist", "Outputs", "Inputs", "Meters", "TempProbes"}
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
        if obj_type == "input":
            base["State"] = component.get("State", False)
            base["Webhooks"] = component.get("Webhooks", False)
        elif obj_type == "output":
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
