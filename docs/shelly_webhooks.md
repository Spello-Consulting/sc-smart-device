# Shelly webhooks

SCSmartDevices supports webhooks for Shelly smart switches. When enabled, a webhook server is started to listen for webhook events posted by a Shelly device. 
For example, your application can be immediately notified when an input switch on a Shelly smart switch is turned on or off. 

The SupportedWebhooks attrbute of a device object lists the webhook events that each device supports (if any). See this page for documentation:
https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Webhook#webhookcreate

To use webhooks you must:

1. Properly configure the [ShellyWebhooks section](example_config.md#shellywebhooks-key) of the SCSmartDevices configuration block.
2. Have your client app running on a system that accepts inbound http connections on the IP address and port configured in the ShellyWebhooks section.
3. Be using a Shelly device (typically Gen 3 or later) that supports wbhooks. 
4. Add the _Webhooks_ key to a device's input of output configuration so that webhook handlers are installed for that component. 

Here's an example application:

```python
  {%
    include "../examples/switch_webhooks.py"
  %}
```

## Pulling specific events from the queue

Incoming webhook events are held in an internal FIFO queue. Calling
`pull_webhook_event()` with no arguments returns and removes the **oldest**
queued event (or `None` when the queue is empty).

If your app only cares about events for a particular device or component, you
can pass one or more optional filters. `pull_webhook_event()` then returns the
**oldest event for which _every_ supplied filter matches**, removing just that
event and leaving the rest of the queue untouched. Unsupplied filters are
ignored, and `None` is returned when no queued event matches.

| Argument         | Type          | Matches when                        |
|------------------|---------------|-------------------------------------|
| `device_id`      | `int \| None`  | `event["Device"]["ID"]` equals it   |
| `device_name`    | `str \| None`  | `event["Device"]["Name"]` equals it |
| `component_id`   | `int \| None`  | `event["Component"]["ID"]` equals it   |
| `component_name` | `str \| None`  | `event["Component"]["Name"]` equals it |

```python
# Oldest event of any kind (unchanged, backward-compatible behaviour)
event = smart_switch_control.pull_webhook_event()

# Oldest event for the device named "Boiler"
event = smart_switch_control.pull_webhook_event(device_name="Boiler")

# Oldest event for one specific output of one specific device (AND)
event = smart_switch_control.pull_webhook_event(device_id=1, component_name="Output 1")
```

The same filtered signature is available on `SCSmartDevice`,
`SmartDeviceWorker` and the underlying `ShellyProvider`.

## Tasmota devices

Tasmota ESP32 devices don't support webhooks, but they do support signalling to a client app via Matter (MQTT) events. This will be supported in a later version of this package.