from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sc_smart_device.models.smart_device_status import SmartDeviceStatus


class BaseProvider(ABC):
    """Abstract base class that every hardware provider must implement.

    SCSmartDevice owns one or more providers and calls this contract to:
    - load device configuration
    - refresh device state
    - control outputs
    - start/stop provider-specific background services (e.g. webhook server)
    - produce a normalized SmartDeviceStatus snapshot
    """

    @abstractmethod
    def initialize_settings(self, provider_config: dict, refresh_status: bool = True) -> None:
        """Load provider config, build internal device list, optionally refresh state."""

    @abstractmethod
    def get_device_status(self, device_identity: dict | int | str) -> bool:
        """Refresh one device's state. Returns True if online, False if offline."""

    @abstractmethod
    def refresh_all_device_statuses(self) -> None:
        """Refresh state for every device owned by this provider."""

    @abstractmethod
    def set_output(self, output_identity: dict | int | str, new_state: bool) -> tuple[bool, bool]:
        """Set an output state. Returns (success, did_change)."""

    @abstractmethod
    def get_device_location(self, device_identity: dict | int | str) -> dict | None:
        """Return timezone / lat / lon dict for device, or None."""

    @abstractmethod
    def start_services(self) -> None:
        """Start any background services (e.g. webhook HTTP server)."""

    @abstractmethod
    def stop_services(self) -> None:
        """Stop background services and release resources."""

    @abstractmethod
    def get_normalized_status(self) -> SmartDeviceStatus:
        """Return a normalized snapshot with provider-agnostic public dicts."""

    @abstractmethod
    def get_device(self, device_identity: dict | int | str) -> dict:
        """Return the internal mutable device dict. Raise RuntimeError if not found."""

    @abstractmethod
    def get_device_component(
        self,
        component_type: str,
        component_identity: int | str,
        use_index: bool | None = None,
    ) -> dict:
        """Return an internal mutable component dict. Raise RuntimeError if not found."""

    @abstractmethod
    def is_device_online(self, device_identity: dict | int | str | None = None) -> bool:
        """Ping device(s) and return True if all are online."""

    @abstractmethod
    def get_device_information(self, device_identity: dict | int | str, refresh_status: bool = False) -> dict:
        """Return a device dict augmented with its component sub-lists."""

    @abstractmethod
    def print_device_status(self, device_identity: int | str | None = None) -> str:
        """Return a human-readable status string for one or all devices."""

    # ── Optional / provider-specific — default no-op implementations ─────────

    def does_device_have_webhooks(self, _device: dict) -> bool:  # noqa: PLR6301
        """Return True if any component of the device has webhooks enabled."""
        return False

    def install_webhook(
        self,
        _event: str,
        _component: dict,
        _url: str | None = None,
        _additional_payload: dict | None = None,
    ) -> None:
        """Install a webhook.

        Raises:
            RuntimeError: If webhooks are not supported by this provider.
        """
        msg = f"{type(self).__name__} does not support webhooks."
        raise RuntimeError(msg)

    def pull_webhook_event(  # noqa: PLR6301
        self,
        _device_id: int | None = None,
        _device_name: str | None = None,
        _component_id: int | None = None,
        _component_name: str | None = None,
    ) -> dict | None:
        """Return the oldest queued webhook event matching the given filters, or None."""
        return None

    def print_model_library(self, _mode_str: str = "brief", _model_id: str | None = None) -> str:  # noqa: PLR6301
        """Return a human-readable summary of supported hardware models."""
        return ""
