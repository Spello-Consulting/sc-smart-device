"""Example of using the SmartDevice control to handle webhooks."""
import platform
import pprint
import sys
import threading
import time

from sc_foundation import SCLogger
from switch_init import switch_init

from sc_smart_device import SCSmartDevice

# Test a Shelly switch
device_identity = "Spello Test"  # Change this to the name of your device in the configuration file

# Note: Webhooks are not supported on Tasmota devices, so no Tasmota test section for this example.


def test_webhooks(logger: SCLogger, smart_switch_control: SCSmartDevice, wake_event: threading.Event):
    """Test function for webhooks SmartDevices control."""
    loop_delay = 2
    loop_count = 0
    max_loops = 20

    logger.log_message(f"\n\n\nTesting webhook functionality for device: {device_identity}", "summary")

    # Get the device
    try:
        device = smart_switch_control.get_device(device_identity)
        device_status = smart_switch_control.get_device_status(device)
        if device_status:
            logger.log_message(f"Device {device_identity} is online.", "summary")
        else:
            logger.log_message(f"Device {device_identity} is offline or not found.", "error")
    except RuntimeError as e:
        logger.log_message(f"Error getting status for device {device_identity}: {e}", "error")
        sys.exit(1)
    except TimeoutError as e:
        logger.log_message(f"Timeout error getting status for device {device_identity}: {e}", "error")
    else:
        logger.log_message(f"{device_identity} initial status:\n {smart_switch_control.print_device_status(device_identity)}", "detailed")

        while loop_count < max_loops:
            logger.log_message(f"Waiting for webhook events... (Loop {loop_count + 1}/{max_loops})", "detailed")

            if wake_event.is_set():
                # We were woken by a webhook call
                while True:
                    event = smart_switch_control.pull_webhook_event()
                    if not event:
                        break

                    event_name = event.get("Event")
                    event_device = event.get("Device", {}).get("Name")
                    event_component = event.get("Component", {}).get("Name")
                    logger.log_message(f"Received webhook event: Name: {event_name}, Device: {event_device}, Component: {event_component}", "detailed")
                    event_str = pprint.pformat(event, indent=2)
                    logger.log_message(f"\nWebhook event detail: {event_str}\n", "debug")
                wake_event.clear()

            time.sleep(loop_delay)
            loop_count += 1


def main():
    """Main function to run the example code."""
    wake_event = threading.Event()

    print(f"Hello from switch_webhooks running on {platform.system()}")

    # Initialize the configuration manager, logger, and SmartDevices control
    try:
        _config, logger, smart_switch_control = switch_init(wake_event=wake_event)
    except RuntimeError as e:
        print(f"Initialization error: {e}", file=sys.stderr)
        sys.exit(1)

    test_webhooks(logger, smart_switch_control, wake_event)


if __name__ == "__main__":
    main()
