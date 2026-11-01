"""Example of using the SmartDevice control to read energy meters."""
import platform
import sys
import time

from sc_foundation import SCLogger
from switch_init import switch_init

from sc_smart_device import SCSmartDevice

# ------- Uncomment the relevant section below to test different devices and meters -------
# Test a Shelly energy Meter
meter_device_name = "Power EM4"
meter1_name = "Power EM4.1"
meter2_name = "Power EM4.2"
meter3_name = "Power EM4.3"


def test_new_meter(logger: SCLogger, smart_switch_control: SCSmartDevice):  # noqa: PLR0914
    """Test SmartDevices energy meter functionality."""
    loop_delay = 1
    loop_count = 0
    max_loops = 5

    logger.log_message(f"Testing meter functionality for device: {meter_device_name}", "summary")

    # Get the device and components
    try:
        meter_device = smart_switch_control.get_device(meter_device_name)
        meter1 = smart_switch_control.get_device_component("meter", meter1_name)
        meter2 = smart_switch_control.get_device_component("meter", meter2_name)
        meter3 = smart_switch_control.get_device_component("meter", meter3_name)
    except RuntimeError as e:
        print(f"Error getting device: {e}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError as e:
        print(f"Timeout error getting device: {e}", file=sys.stderr)
        sys.exit(1)
    else:
        logger.log_message(f"{meter_device_name} status:\n {smart_switch_control.print_device_status(meter_device_name)}", "detailed")

        while loop_count < max_loops:
            # Refresh the status of all devices
            smart_switch_control.get_device_status(meter_device)

            meter1_volts = meter1.get("Voltage", False)
            meter1_power = meter1.get("Power", False)
            meter1_energy = meter1.get("Energy", False)
            meter2_volts = meter2.get("Voltage", False)
            meter2_power = meter2.get("Power", False)
            meter2_energy = meter2.get("Energy", False)
            meter3_volts = meter3.get("Voltage", False)
            meter3_power = meter3.get("Power", False)
            meter3_energy = meter3.get("Energy", False)

            logger.log_message(f"{meter1_name} Volts: {meter1_volts}, Power: {meter1_power}, Energy: {meter1_energy}.", "detailed")
            logger.log_message(f"{meter2_name} Volts: {meter2_volts}, Power: {meter2_power}, Energy: {meter2_energy}.", "detailed")
            logger.log_message(f"{meter3_name} Volts: {meter3_volts}, Power: {meter3_power}, Energy: {meter3_energy}.", "detailed")

            time.sleep(loop_delay)
            loop_count += 1


def main():
    """Main function to run the example code."""
    print(f"Hello from switch_meter running on {platform.system()}")

    # Initialize the configuration manager, logger, and SmartDevices control
    try:
        _config, logger, smart_switch_control = switch_init()
    except RuntimeError as e:
        print(f"Initialization error: {e}", file=sys.stderr)
        sys.exit(1)

    test_new_meter(logger, smart_switch_control)


if __name__ == "__main__":
    main()
