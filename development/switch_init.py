"""Manual testing code for the ShellyControl class."""

import threading

from mergedeep import merge
from sc_foundation import (
    SCCommon,
    SCConfigManager,
    SCLogger,
)

from sc_smart_device import SCSmartDevice, smart_devices_validator

CONFIG_FILE = "configs/dev_tasmota.yaml"


def switch_init(wake_event: threading.Event | None = None, extra_validation: dict | None = None) -> tuple[SCConfigManager, SCLogger, SCSmartDevice]:
    """Create an instance of the SCConfigManager, SCLogger and SCSmartDevice class.

    Args:
        wake_event (threading.Event | None): Optional threading event to signal webhook events.
        extra_validation (dict | None): Optional extra validation schema to merge with the default schema.

    Returns:
        tuple[SCConfigManager, SCLogger, SCSmartDevice]: A tuple containing the initialized SCConfigManager, SCLogger, and SCSmartDevice instances.

    Raises:
        RuntimeError: If there is an error with the configuration file, logger initialization, or SCSmartDevice initialization.
    """
    # Merge the SmartDevices validation schema with the default validation schema
    merged_schema = merge({}, smart_devices_validator, extra_validation or {})
    assert isinstance(merged_schema, dict), "Merged schema should be type dict"

    # Initialize the SC_ConfigManager class
    try:
        config = SCConfigManager(
            config_file=CONFIG_FILE,
            validation_schema=merged_schema,
        )
    except RuntimeError as e:
        error_msg = f"Configuration file error: {e}"
        raise RuntimeError(error_msg) from e

    # Initialize the SC_Logger class
    try:
        logger_settings = config.get_logger_settings()
        logger = SCLogger(logger_settings)
    except RuntimeError as e:
        error_msg = f"Logger initialisation error: {e}"
        raise RuntimeError(error_msg) from e

    # Test internet connection
    if not SCCommon.check_internet_connection():
        logger.log_message("No internet connection detected.", "error")

    smart_switch_settings = config.get("SCSmartDevices")

    if smart_switch_settings is None:
        error_msg = "No SmartDevices settings found in the configuration file."
        raise RuntimeError(error_msg)

    # Initialize the SCSmartDevice class
    try:
        smart_switch_control = SCSmartDevice(logger, smart_switch_settings, wake_event)
    except RuntimeError as e:
        error_msg = f"SCSmartDevice initialization error: {e}"
        raise RuntimeError(error_msg) from e
    logger.log_message(f"SCSmartDevice initialized successfully with {len(smart_switch_control.devices)} devices.", "summary")

    return config, logger, smart_switch_control
