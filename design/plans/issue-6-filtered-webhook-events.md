# Issue #6 — Pull specific webhook events from the queue

**Issue:** [Allow specific webhook events to be pulled from the queue](https://github.com/Spello-Consulting/sc-smart-device/issues/6)

## Goal

Let the client app pop the **earliest** queued webhook event that matches a
device/component filter, instead of always popping the absolute earliest event.

Filter criteria:

- `Device.ID` = `<int>`
- `Device.Name` = `<str>`
- `Component.ID` = `<int>`
- `Component.Name` = `<str>`

## Decisions (confirmed)

1. **API shape** — *extend* the existing `pull_webhook_event()` with optional
   keyword filters. Fully backward compatible: called with no args it behaves
   exactly as today (pops the absolute earliest event). Only one method to keep
   in sync across the three layers.
2. **Match logic** — when more than one filter is supplied, they combine with
   **AND** (every supplied filter must match). Unsupplied filters are ignored.

## How events are structured (context)

Events are appended FIFO in `ShellyProvider._push_webhook_event()`
([shelly_provider.py:927](../../src/sc_smart_device/providers/shelly_provider.py)).
Each event dict may contain:

- `timestamp`
- `Device` — the resolved device dict (`ID`, `Name`, …) *if* the device was found
- `Component` — the resolved component dict (`ID`, `Name`, …) *if* it was found
- other raw args (`Event`, `path`, …)

Note `Device`/`Component` are only present when resolution succeeded, so the
matcher must tolerate their absence.

## New signature

```python
def pull_webhook_event(
    self,
    device_id: int | None = None,
    device_name: str | None = None,
    component_id: int | None = None,
    component_name: str | None = None,
) -> dict | None:
```

Semantics: scan the queue oldest→newest, return and remove the **first** event
where every *supplied* filter matches. If no filters are supplied, pop index 0
(current behaviour). Returns `None` if nothing matches.

## Matching rule (per event)

An event matches a given filter when:

| Filter           | Matches when                                   |
|------------------|------------------------------------------------|
| `device_id`      | `event["Device"]["ID"] == device_id`           |
| `device_name`    | `event["Device"]["Name"] == device_name`       |
| `component_id`   | `event["Component"]["ID"] == component_id`      |
| `component_name` | `event["Component"]["Name"] == component_name`  |

If a filter is supplied but the event lacks the relevant sub-dict/key, that
event does **not** match. `None` filters are skipped entirely.

## Files to change

### 1. `src/sc_smart_device/providers/shelly_provider.py`
- Replace `pull_webhook_event()` (line ~276) with the filtered version.
- Add a small private helper `_event_matches(event, ...) -> bool` for the
  per-event AND check (keeps the loop readable, and reusable/testable).

### 2. `src/sc_smart_device/providers/base_provider.py`
- Update the default no-op `pull_webhook_event()` (line ~99) to accept the same
  optional kwargs so the abstract/base contract matches subclasses. Still
  returns `None`.

### 3. `src/sc_smart_device/providers/tasmota_provider.py`
- Update the stub signature (line ~186) to match the base contract. (Tasmota has
  no webhook queue, so still returns `None`.)

### 4. `src/sc_smart_device/smart_device.py`
- `SCSmartDevice.pull_webhook_event()` (line ~503): accept the kwargs and pass
  them through to each provider. Preserve the current "first provider with an
  event wins" loop, but only count a provider's result when it matches the
  filter (i.e. pass filters down to `provider.pull_webhook_event(...)`).
- Update the Google-style docstring (Args section).

### 5. `src/sc_smart_device/smart_device_worker.py`
- `SmartDeviceWorker.pull_webhook_event()` (line ~252): accept and forward the
  kwargs. Update the docstring.

## Tests (`tests/`)

Add cases (likely in a new `tests/test_webhooks.py` or existing worker/basic
test module, following current fixture patterns):

- No-filter pop returns absolute earliest (regression / backward-compat).
- Filter by `device_id` skips non-matching earlier events, pops the right one.
- Filter by `device_name`, `component_id`, `component_name` each individually.
- Combined filters use AND (matches only when all match; a partial match is
  skipped).
- No match → returns `None` and leaves the queue untouched.
- Event missing `Device`/`Component` sub-dict is safely skipped when that filter
  is supplied.
- Propagation: same behaviour observable through `SCSmartDevice` and
  `SmartDeviceWorker`.

## Docs

- Update `docs/shelly_webhooks.md` to document the new optional filter args on
  `pull_webhook_event()` with a short example (e.g. pull the next event for one
  named boiler output).
- Update the two examples that call `pull_webhook_event()`
  (`examples/switch_webhooks.py`, `examples/simple_example.py`) only if a
  filtered example adds clarity — otherwise leave as-is since the call stays
  backward compatible.

## Quality gate

- `ruff format` + `ruff check` clean.
- `mypy --strict` clean (all new params typed; `int | None` / `str | None`).
- `uv run pytest` green.
