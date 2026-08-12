"""Import smoke test.

A config flow that fails to import shows up in the UI only as the generic
"Config flow could not be loaded: Invalid handler specified." Walking the whole
import chain here catches a missing const, a stale reference, or a typo before
it turns into that message.
"""

from __future__ import annotations

import importlib

MODULES = [
    "sf.const",
    "sf.api",
    "sf.coordinator",
    "sf.config_flow",
    "sf.device_tracker",
    "sf.sensor",
]


def test_every_module_imports() -> None:
    for name in MODULES:
        assert importlib.import_module(name) is not None


def test_package_init_imports() -> None:
    """The package __init__ is the first thing HA loads, so check it too.

    It is loaded by path because the test harness exposes the component directory
    as a synthetic package rather than executing this file on import.
    """
    import importlib.util
    import sys
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "stopfinder"
        / "__init__.py"
    )
    spec = importlib.util.spec_from_file_location("sf._init", path)
    module = importlib.util.module_from_spec(spec)
    # Relative imports resolve against sys.modules, so register before executing.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert callable(module.async_setup_entry)
    assert callable(module.async_unload_entry)


def test_config_flow_handler_is_registered_for_the_domain() -> None:
    const = importlib.import_module("sf.const")
    config_flow = importlib.import_module("sf.config_flow")

    handler = config_flow.StopfinderConfigFlow
    assert const.DOMAIN == "stopfinder"
    # The steps HA needs by name: without reauth_confirm, a rejected password
    # leaves the entry stuck instead of prompting for a new one.
    for step in ("async_step_user", "async_step_reauth", "async_step_reauth_confirm"):
        assert callable(getattr(handler, step))


def test_sensor_states_are_all_declared_as_enum_options() -> None:
    const = importlib.import_module("sf.const")
    sensor = importlib.import_module("sf.sensor")

    [status] = [d for d in sensor.SENSORS if d.key == "gps_status"]
    assert set(status.options) == {
        const.GPS_VALID,
        const.GPS_SEARCHING,
        const.GPS_NOT_AVAILABLE,
        const.GPS_NO_VEHICLE,
    }


def test_declared_sensor_states_have_translations() -> None:
    """Every enum option needs a translation, or HA logs an unknown state."""
    import json
    from pathlib import Path

    const = importlib.import_module("sf.const")
    component = Path(__file__).resolve().parents[1] / "custom_components" / "stopfinder"

    for path in (component / "strings.json", component / "translations" / "en.json"):
        data = json.loads(path.read_text())
        states = data["entity"]["sensor"]["gps_status"]["state"]
        assert set(states) == set(const.GPS_STATUSES), path


def test_every_sensor_translation_key_has_a_name() -> None:
    """A translation_key with no entry renders as a blank entity name in HA."""
    import json
    from pathlib import Path

    sensor = importlib.import_module("sf.sensor")
    component = Path(__file__).resolve().parents[1] / "custom_components" / "stopfinder"

    keys = {d.translation_key for d in sensor.SENSORS}
    keys |= {d.translation_key for d in sensor.ANNOUNCEMENT_SENSORS}
    assert None not in keys, "every sensor description needs a translation_key"

    for path in (component / "strings.json", component / "translations" / "en.json"):
        names = json.loads(path.read_text())["entity"]["sensor"]
        missing = {k for k in keys if not names.get(k, {}).get("name")}
        assert not missing, f"{path.name} is missing names for {sorted(missing)}"


def test_options_schema_keys_have_labels() -> None:
    """An option with no label shows its raw key in the options dialog."""
    import json
    from pathlib import Path

    const = importlib.import_module("sf.const")
    component = Path(__file__).resolve().parents[1] / "custom_components" / "stopfinder"

    expected = {
        const.CONF_GPS_POLL_SECONDS,
        const.CONF_GEO_ALERT_POLL_SECONDS,
        const.CONF_ANNOUNCEMENT_POLL_MINUTES,
        const.CONF_ANNOUNCEMENT_LEAD_HOURS,
        const.CONF_ANNOUNCEMENT_TRAIL_HOURS,
        const.CONF_SCHEDULE_TICK_SECONDS,
    }
    for path in (component / "strings.json", component / "translations" / "en.json"):
        labels = json.loads(path.read_text())["options"]["step"]["init"]["data"]
        assert expected <= set(labels), path.name
