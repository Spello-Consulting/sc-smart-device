"""Webhook queue filtering tests for pull_webhook_event().

Covers the ShellyProvider matcher directly plus propagation through the
SCSmartDevice and SmartDeviceWorker layers (issue #6).
"""

import threading

from sc_fixtures import logger, smart_device

from sc_smart_device import SmartDeviceWorker
from sc_smart_device.providers.shelly_provider import ShellyProvider


def _event(marker: str, device_id: int, device_name: str, comp_id: int, comp_name: str) -> dict:
    """Build a synthetic queued event dict with the fields the matcher inspects.

    Returns:
        An event dict with ``Device`` and ``Component`` sub-dicts.
    """
    return {
        "timestamp": marker,
        "Event": marker,
        "Device": {"ID": device_id, "Name": device_name},
        "Component": {"ID": comp_id, "Name": comp_name},
    }


def _seeded_provider() -> ShellyProvider:
    """Return a fresh provider whose queue holds three ordered events.

    Returns:
        A ShellyProvider seeded with three events (first, second, third).
    """
    provider = ShellyProvider(logger, threading.Event())
    provider.webhook_event_queue = [
        _event("first", 1, "Boiler", 10, "Output A"),
        _event("second", 2, "Pump", 20, "Output B"),
        _event("third", 1, "Boiler", 11, "Output C"),
    ]
    return provider


# ── Backward compatibility ────────────────────────────────────────────────────

def test_no_filter_pops_earliest():
    """With no filters, the absolute earliest event is returned and removed."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event()
    assert event is not None
    assert event["Event"] == "first"
    assert len(provider.webhook_event_queue) == 2


def test_empty_queue_returns_none():
    """An empty queue returns None regardless of filters."""
    provider = ShellyProvider(logger, threading.Event())
    assert provider.pull_webhook_event() is None
    assert provider.pull_webhook_event(device_id=1) is None


# ── Single-filter matching ────────────────────────────────────────────────────

def test_filter_by_device_id_skips_non_matching():
    """device_id skips earlier non-matching events and pops the right one."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(device_id=2)
    assert event is not None
    assert event["Event"] == "second"
    # Only the matched event is removed; the others stay in order.
    assert [e["Event"] for e in provider.webhook_event_queue] == ["first", "third"]


def test_filter_by_device_name():
    """device_name returns the earliest event for that device."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(device_name="Boiler")
    assert event is not None
    assert event["Event"] == "first"


def test_filter_by_component_id():
    """component_id returns the earliest event for that component."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(component_id=11)
    assert event is not None
    assert event["Event"] == "third"


def test_filter_by_component_name():
    """component_name returns the earliest event for that component."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(component_name="Output B")
    assert event is not None
    assert event["Event"] == "second"


# ── Combined filters (AND) ────────────────────────────────────────────────────

def test_combined_filters_use_and():
    """All supplied filters must match; the first fully matching event wins."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(device_name="Boiler", component_name="Output C")
    assert event is not None
    assert event["Event"] == "third"


def test_partial_match_is_skipped():
    """An event matching only some filters is not returned."""
    provider = _seeded_provider()
    # device 1 exists and component 20 exists, but not on the same event.
    event = provider.pull_webhook_event(device_id=1, component_id=20)
    assert event is None
    assert len(provider.webhook_event_queue) == 3


# ── No match / missing sub-dicts ──────────────────────────────────────────────

def test_no_match_returns_none_and_preserves_queue():
    """A filter matching nothing returns None and leaves the queue intact."""
    provider = _seeded_provider()
    event = provider.pull_webhook_event(device_id=999)
    assert event is None
    assert len(provider.webhook_event_queue) == 3


def test_event_missing_component_is_skipped_when_filtering_component():
    """An event without a Component sub-dict is skipped when a component filter is set."""
    provider = ShellyProvider(logger, threading.Event())
    provider.webhook_event_queue = [
        {"timestamp": "bare", "Event": "bare", "Device": {"ID": 1, "Name": "Boiler"}},
        _event("full", 1, "Boiler", 10, "Output A"),
    ]
    event = provider.pull_webhook_event(component_id=10)
    assert event is not None
    assert event["Event"] == "full"


# ── Propagation through the upper layers ──────────────────────────────────────

def test_propagation_through_smart_device_and_worker():
    """Filters reach the provider through SCSmartDevice and SmartDeviceWorker."""
    provider = smart_device._providers[0]  # noqa: SLF001
    assert isinstance(provider, ShellyProvider)
    saved_queue = provider.webhook_event_queue
    try:
        provider.webhook_event_queue = [
            _event("p_first", 5, "Alpha", 50, "Out X"),
            _event("p_second", 6, "Beta", 60, "Out Y"),
        ]

        # Via SCSmartDevice
        event = smart_device.pull_webhook_event(device_name="Beta")
        assert event is not None
        assert event["Event"] == "p_second"

        # Via SmartDeviceWorker (re-seed one matching event)
        provider.webhook_event_queue = [_event("w_only", 7, "Gamma", 70, "Out Z")]
        worker = SmartDeviceWorker(smart_device, logger, threading.Event())
        event = worker.pull_webhook_event(component_id=70)
        assert event is not None
        assert event["Event"] == "w_only"
        assert provider.webhook_event_queue == []
    finally:
        provider.webhook_event_queue = saved_queue
