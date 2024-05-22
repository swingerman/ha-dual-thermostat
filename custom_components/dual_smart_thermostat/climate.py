"""Adds support for dual smart thermostat units."""

import asyncio
import logging

from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_NONE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    STATE_ON,
    STATE_OPEN,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.service import extract_entity_ids
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import voluptuous as vol

from custom_components.dual_smart_thermostat.hvac_action_reason.hvac_action_reason_external import (
    HVACActionReasonExternal,
)
from custom_components.dual_smart_thermostat.hvac_device.controllable_hvac_device import (
    ControlableHVACDevice,
)
from custom_components.dual_smart_thermostat.hvac_device.hvac_device_factory import (
    HVACDeviceFactory,
)
from custom_components.dual_smart_thermostat.managers.environment_manager import (
    EnvironmentManager,
    TargetTemperatures,
)
from custom_components.dual_smart_thermostat.managers.feature_manager import (
    FeatureManager,
)
from custom_components.dual_smart_thermostat.managers.opening_manager import (
    OpeningHvacModeScope,
    OpeningManager,
)
from custom_components.dual_smart_thermostat.managers.preset_manager import (
    PresetManager,
)

from . import DOMAIN, PLATFORMS
from .const import (
    ATTR_HVAC_ACTION_REASON,
    ATTR_PREV_TARGET,
    ATTR_PREV_TARGET_HIGH,
    ATTR_PREV_TARGET_LOW,
    ATTR_TIMEOUT,
    CONF_AC_MODE,
    CONF_AUX_HEATER,
    CONF_AUX_HEATING_DUAL_MODE,
    CONF_AUX_HEATING_TIMEOUT,
    CONF_COLD_TOLERANCE,
    CONF_COOLER,
    CONF_DRY_TOLERANCE,
    CONF_DRYER,
    CONF_FAN,
    CONF_FAN_AIR_OUTSIDE,
    CONF_FAN_HOT_TOLERANCE,
    CONF_FAN_MODE,
    CONF_FAN_ON_WITH_AC,
    CONF_FLOOR_SENSOR,
    CONF_HEAT_COOL_MODE,
    CONF_HEATER,
    CONF_HOT_TOLERANCE,
    CONF_HUMIDITY_SENSOR,
    CONF_INITIAL_HVAC_MODE,
    CONF_KEEP_ALIVE,
    CONF_MAX_FLOOR_TEMP,
    CONF_MAX_HUMIDITY,
    CONF_MAX_TEMP,
    CONF_MIN_DUR,
    CONF_MIN_FLOOR_TEMP,
    CONF_MIN_HUMIDITY,
    CONF_MIN_TEMP,
    CONF_MOIST_TOLERANCE,
    CONF_OPENINGS,
    CONF_OPENINGS_SCOPE,
    CONF_OUTSIDE_SENSOR,
    CONF_PRECISION,
    CONF_PRESETS,
    CONF_PRESETS_OLD,
    CONF_SENSOR,
    CONF_SENSOR_SAFETY_DELAY,
    CONF_TARGET_HUMIDITY,
    CONF_TARGET_TEMP,
    CONF_TARGET_TEMP_HIGH,
    CONF_TARGET_TEMP_LOW,
    CONF_TEMP_STEP,
    DEFAULT_MAX_FLOOR_TEMP,
    DEFAULT_NAME,
    DEFAULT_TOLERANCE,
    TIMED_OPENING_SCHEMA,
)
from .hvac_action_reason.hvac_action_reason import (
    SERVICE_SET_HVAC_ACTION_REASON,
    SET_HVAC_ACTION_REASON_SIGNAL,
    HVACActionReason,
)

_LOGGER = logging.getLogger(__name__)

PRESET_SCHEMA = {
    vol.Optional(ATTR_TEMPERATURE): vol.Coerce(float),
    vol.Optional(ATTR_TARGET_TEMP_LOW): vol.Coerce(float),
    vol.Optional(ATTR_TARGET_TEMP_HIGH): vol.Coerce(float),
}

SECONDARY_HEATING_SCHEMA = {
    vol.Optional(CONF_AUX_HEATER): cv.entity_id,
    vol.Optional(CONF_AUX_HEATING_DUAL_MODE): cv.boolean,
    vol.Optional(CONF_AUX_HEATING_TIMEOUT): vol.All(
        cv.time_period, cv.positive_timedelta
    ),
}

FLOOR_TEMPERATURE_SCHEMA = {
    vol.Optional(CONF_FLOOR_SENSOR): cv.entity_id,
    vol.Optional(CONF_MAX_FLOOR_TEMP): vol.Coerce(float),
    vol.Optional(CONF_MIN_FLOOR_TEMP): vol.Coerce(float),
}

FAN_MODE_SCHEMA = {
    vol.Optional(CONF_FAN): cv.entity_id,
    vol.Optional(CONF_FAN_MODE): cv.boolean,
    vol.Optional(CONF_FAN_ON_WITH_AC): cv.boolean,
    vol.Optional(CONF_FAN_HOT_TOLERANCE): vol.Coerce(float),
    vol.Optional(CONF_FAN_AIR_OUTSIDE): cv.boolean,
}

OPENINGS_SCHEMA = {
    vol.Optional(CONF_OPENINGS): [vol.Any(cv.entity_id, TIMED_OPENING_SCHEMA)],
    vol.Optional(CONF_OPENINGS_SCOPE): vol.Any(
        OpeningHvacModeScope, [scope.value for scope in OpeningHvacModeScope]
    ),
}

HYGROSTAT_SCHEMA = {
    vol.Optional(CONF_DRYER): cv.entity_id,
    vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
    vol.Optional(CONF_MIN_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_MAX_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_TARGET_HUMIDITY): vol.Coerce(float),
    vol.Optional(CONF_DRY_TOLERANCE): vol.Coerce(float),
    vol.Optional(CONF_MOIST_TOLERANCE): vol.Coerce(float),
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Optional(CONF_COOLER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_SENSOR_SAFETY_DELAY): vol.All(
            cv.time_period, cv.positive_timedelta
        ),
        vol.Optional(CONF_OUTSIDE_SENSOR): cv.entity_id,
        vol.Optional(CONF_AC_MODE): cv.boolean,
        vol.Optional(CONF_HEAT_COOL_MODE): cv.boolean,
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MIN_DUR): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_HIGH): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP_LOW): vol.Coerce(float),
        vol.Optional(CONF_KEEP_ALIVE): vol.All(cv.time_period, cv.positive_timedelta),
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [
                HVACMode.COOL,
                HVACMode.HEAT,
                HVACMode.OFF,
                HVACMode.HEAT_COOL,
                HVACMode.FAN_ONLY,
            ]
        ),
        vol.Optional(CONF_PRECISION): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_TEMP_STEP): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    }
).extend({vol.Optional(v): PRESET_SCHEMA for (k, v) in CONF_PRESETS.items()})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(SECONDARY_HEATING_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(FLOOR_TEMPERATURE_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(OPENINGS_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(FAN_MODE_SCHEMA)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(HYGROSTAT_SCHEMA)

# Add the old presets schema to avoid breaking change
PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {vol.Optional(v): vol.Coerce(float) for (k, v) in CONF_PRESETS_OLD.items()}
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the smart dual thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config[CONF_NAME]
    sensor_entity_id = config[CONF_SENSOR]
    sensor_floor_entity_id = config.get(CONF_FLOOR_SENSOR)
    sensor_outside_entity_id = config.get(CONF_OUTSIDE_SENSOR)
    sensor_humidity_entity_id = config.get(CONF_HUMIDITY_SENSOR)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    presets_dict = {
        key: config[value] for key, value in CONF_PRESETS.items() if value in config
    }
    _LOGGER.debug("Presets dict: %s", presets_dict)
    presets = {
        key: values[ATTR_TEMPERATURE]
        for key, values in presets_dict.items()
        if ATTR_TEMPERATURE in values
    }
    _LOGGER.debug("Presets: %s", presets)

    # Try to load presets in old format and use if new format not available in config
    old_presets = {k: config[v] for k, v in CONF_PRESETS_OLD.items() if v in config}
    if old_presets:
        _LOGGER.warning(
            "Found deprecated presets settings in configuration. "
            "Please remove and replace with new presets settings format. "
            "Read documentation in integration repository for more details"
        )
        if not presets_dict:
            presets = old_presets

    precision = config.get(CONF_PRECISION)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)

    opening_manager = OpeningManager(hass, config)

    environment_manager = EnvironmentManager(
        hass,
        config,
        presets,
    )

    feature_manager = FeatureManager(hass, config, environment_manager)

    preset_manager = PresetManager(hass, config, environment_manager, feature_manager)

    # device_factory = HVACDeviceFactory(hass, config, feature_manager)
    device_factory = HVACDeviceFactory(hass, config, feature_manager)

    hvac_device = device_factory.create_device(environment_manager, opening_manager)

    async_add_entities(
        [
            DualSmartThermostat(
                name,
                sensor_entity_id,
                sensor_floor_entity_id,
                sensor_outside_entity_id,
                sensor_humidity_entity_id,
                keep_alive,
                precision,
                unit,
                unique_id,
                hvac_device,
                preset_manager,
                environment_manager,
                opening_manager,
                feature_manager,
            )
        ]
    )

    # Service to set HVACActionReason.
    def set_hvac_action_reason_service(call: ServiceCall) -> None:
        """My first service."""
        _LOGGER.debug("Received data %s", call.data)
        reason = call.data.get(ATTR_HVAC_ACTION_REASON)
        entity_ids = extract_entity_ids(hass, call)

        # make sure its a valid external reason
        if reason not in HVACActionReasonExternal:
            _LOGGER.error("Invalid HVACActionReasonExternal: %s", reason)
            return

        if entity_ids:
            # registry:EntityRegistry = await async_get_registry(hass)
            for entity_id in entity_ids:
                _LOGGER.debug(
                    "SETTING HVAC ACTION REASON %s for entity: %s", reason, entity_id
                )

                async_dispatcher_send(
                    hass, SET_HVAC_ACTION_REASON_SIGNAL.format(entity_id), reason
                )

    # Register HVACActionReason service with Home Assistant.
    hass.services.async_register(
        DOMAIN, SERVICE_SET_HVAC_ACTION_REASON, set_hvac_action_reason_service
    )


class DualSmartThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Dual Smart Thermostat device."""

    def __init__(
        self,
        name,
        sensor_entity_id,
        sensor_floor_entity_id,
        sensor_outside_entity_id,
        sensor_humidity_entity_id,
        keep_alive,
        precision,
        unit,
        unique_id,
        hvac_device: ControlableHVACDevice,
        preset_manager: PresetManager,
        environment_manager: EnvironmentManager,
        opening_manager: OpeningManager,
        feature_manager: FeatureManager,
    ) -> None:
        """Initialize the thermostat."""
        self._attr_name = name
        self._attr_unique_id = unique_id

        # hvac device
        self.hvac_device: ControlableHVACDevice = hvac_device
        self.hvac_device.set_context(self._context)

        # preset manager
        self.presets = preset_manager

        # temperature manager
        self.environment = environment_manager

        # feature manager
        self.features = feature_manager

        # opening manager
        self.openings = opening_manager

        # sensors
        self.sensor_entity_id = sensor_entity_id
        self.sensor_floor_entity_id = sensor_floor_entity_id
        self.sensor_outside_entity_id = sensor_outside_entity_id
        self.sensor_humidity_entity_id = sensor_humidity_entity_id

        self._keep_alive = keep_alive

        # environment
        self._temp_precision = precision
        self._target_temp = self.environment.target_temp
        self._target_temp_high = self.environment.target_temp_high
        self._target_temp_low = self.environment.target_temp_low
        self._attr_temperature_unit = unit

        self._unit = unit

        # HVAC modes
        self._attr_hvac_modes = self.hvac_device.hvac_modes
        self._hvac_mode = self.hvac_device.hvac_mode
        self._last_hvac_mode = None

        # presets
        self._enable_turn_on_off_backwards_compatibility = False
        self._attr_preset_mode = preset_manager.preset_mode
        self._attr_supported_features = self.features.supported_features
        self._attr_preset_modes = preset_manager.preset_modes

        # hvac action reason
        self._hvac_action_reason = HVACActionReason.NONE
        self._remove_signal_hvac_action_reason = None

        self._temp_lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.sensor_entity_id], self._async_sensor_changed
            )
        )

        switch_entities = self.hvac_device.get_device_ids()
        if switch_entities:
            _LOGGER.info("Adding switch listener: %s", switch_entities)
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, switch_entities, self._async_switch_changed
                )
            )

        # registre device's on-remove
        self.async_on_remove(self.hvac_device.call_on_remove_callbacks)

        if self.sensor_floor_entity_id is not None:
            _LOGGER.debug(
                "Adding floor sensor listener: %s", self.sensor_floor_entity_id
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.sensor_floor_entity_id],
                    self._async_sensor_floor_changed,
                )
            )

        if self.sensor_outside_entity_id is not None:
            _LOGGER.debug(
                "Adding outside sensor listener: %s", self.sensor_outside_entity_id
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.sensor_outside_entity_id],
                    self._async_sensor_outside_changed,
                )
            )

        if self.sensor_humidity_entity_id is not None:
            _LOGGER.debug(
                "Adding humidity sensor listener: %s", self.sensor_humidity_entity_id
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.sensor_humidity_entity_id],
                    self._async_sensor_humidity_changed,
                )
            )

        if self._keep_alive:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_control_climate, self._keep_alive
                )
            )

        if self.openings.opening_entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    self.openings.opening_entities,
                    self._async_opening_changed,
                )
            )

        _LOGGER.info(
            "Setting up signal: %s",
            SET_HVAC_ACTION_REASON_SIGNAL.format(self.entity_id),
        )
        self._remove_signal_hvac_action_reason = async_dispatcher_connect(
            # The Hass Object
            self.hass,
            # The Signal to listen for.
            # Try to make it unique per entity instance
            # so include something like entity_id
            # or other unique data from the service call
            SET_HVAC_ACTION_REASON_SIGNAL.format(self.entity_id),
            # Function handle to call when signal is received
            self._set_hvac_action_reason,
        )

        @callback
        async def _async_startup(*_) -> None:
            """Init on startup."""

            sensor_state = self.hass.states.get(self.sensor_entity_id)
            if self.sensor_floor_entity_id:
                floor_sensor_state = self.hass.states.get(self.sensor_floor_entity_id)
            else:
                floor_sensor_state = None

            if sensor_state and sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self.environment.update_temp_from_state(sensor_state)
                self.async_write_ha_state()

            if floor_sensor_state and floor_sensor_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self.environment.update_floor_temp_from_state(floor_sensor_state)
                self.async_write_ha_state()

            await self.hvac_device.async_on_startup()

        if self.hass.state == CoreState.running:
            await _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        if (old_state := await self.async_get_last_state()) is not None:
            # If we have no initial temperature, restore
            self.environment.apply_old_state(old_state)

            hvac_mode = self._hvac_mode or old_state.state or HVACMode.OFF

            if hvac_mode not in self.hvac_modes:
                hvac_mode = HVACMode.OFF

            self.features.apply_old_state(
                old_state, hvac_mode, self.presets.presets_range
            )
            self._attr_supported_features = self.features.supported_features

            self.environment.set_default_target_temps(
                self.features.is_target_mode,
                self.features.is_range_mode,
                self.hvac_device.hvac_modes,
            )

            # restore previous preset mode if available
            # TODO: completely handle in peset manager
            old_pres_mode = old_state.attributes.get(ATTR_PRESET_MODE)
            if self.features.is_range_mode:
                if (
                    self._attr_preset_modes
                    and old_pres_mode in self.presets.presets_range
                ):
                    self._attr_preset_mode = old_pres_mode

            elif self._attr_preset_modes and old_pres_mode in self.presets.presets:
                _LOGGER.debug("Restoring previous preset mode: %s", old_pres_mode)
                self._attr_preset_mode = old_pres_mode

            self.presets.apply_old_state(old_state)

            _LOGGER.debug("restoring hvac_mode: %s", hvac_mode)
            await self.async_set_hvac_mode(hvac_mode)

            self._hvac_action_reason = old_state.attributes.get(ATTR_HVAC_ACTION_REASON)

        else:
            # No previous state, try and restore defaults
            _LOGGER.debug("No previous state found, setting defaults")
            if not self.hvac_device.hvac_mode:
                self.hvac_device.hvac_mode = HVACMode.OFF
            if self.hvac_device.hvac_mode == HVACMode.OFF:
                self.environment.set_default_target_temps(
                    self.features.is_target_mode,
                    self.features.is_range_mode,
                    self.hvac_device.hvac_modes,
                )

            if self.environment.max_floor_temp is None:
                self.environment.max_floor_temp = DEFAULT_MAX_FLOOR_TEMP

        # Set correct support flag
        self._set_support_flags()

        # Reads sensor and triggers an initial control of climate
        sensor_state = self.hass.states.get(self.sensor_entity_id)
        if sensor_state and sensor_state.state not in (
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
        ):
            self.environment.update_temp_from_state(sensor_state)
            self._target_temp = self.environment.target_temp
            self.async_write_ha_state()

        await self._async_control_climate(force=True)

    async def async_will_remove_from_hass(self) -> None:
        """Call when entity will be removed from hass."""
        if self._remove_signal_hvac_action_reason:
            self._remove_signal_hvac_action_reason()

    @property
    def should_poll(self) -> bool:
        """Return the polling state."""
        return False

    @property
    def precision(self) -> float:
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        if self.environment.target_temperature_step is not None:
            return self.environment.target_temperature_step
        # if a target_temperature_step is not defined, fallback to equal the precision
        return self.precision

    @property
    def current_temperature(self) -> float | None:
        """Return the sensor temperature."""
        return self.environment.cur_temp

    @property
    def current_humidity(self) -> float | None:
        """Return the sensor humidity."""
        return self.environment.cur_humidity

    @property
    def current_floor_temperature(self) -> float | None:
        """Return the sensor temperature."""
        return self.environment.cur_floor_temp

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current operation."""
        return self.hvac_device.hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current running hvac operation if supported."""
        return self.hvac_device.hvac_action

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self.environment.target_temp

    @property
    def target_temperature_high(self) -> float | None:
        """Return the upper bound temperature."""
        return self.environment.target_temp_high

    @property
    def target_temperature_low(self) -> float | None:
        """Return the lower bound temperature."""
        return self.environment.target_temp_low

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        if self.environment.min_temp is not None:
            return self.environment.min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        if self.environment.max_temp is not None:
            return self.environment.max_temp

        # Get default temp from super class
        return super().max_temp

    @property
    def min_humidity(self) -> float:
        """Return the minimum humidity."""
        if self.environment.min_humidity is not None:
            return self.environment.min_humidity

        # get default from super class
        return super().min_humidity

    @property
    def max_humidity(self) -> float:
        """Return the maximum humidity."""
        if self.environment.max_humidity is not None:
            return self.environment.max_humidity

        # get default from supe rclass
        return super().max_humidity

    @property
    def extra_state_attributes(self) -> dict:
        """Return entity specific state attributes to be saved."""

        attributes = {}
        if self._target_temp_low is not None:
            if self._attr_preset_mode != PRESET_NONE and self.features.is_range_mode:
                attributes[ATTR_PREV_TARGET_LOW] = (
                    self.environment.saved_target_temp_low
                )
            else:
                attributes[ATTR_PREV_TARGET_LOW] = self.environment.target_temp_low
        if self._target_temp_high is not None:
            if self._attr_preset_mode != PRESET_NONE and self.features.is_range_mode:
                attributes[ATTR_PREV_TARGET_HIGH] = (
                    self.environment.saved_target_temp_high
                )
            else:
                attributes[ATTR_PREV_TARGET_HIGH] = self.environment.target_temp_high
        if self._target_temp is not None:
            if self._attr_preset_mode != PRESET_NONE and self.features.is_target_mode:
                attributes[ATTR_PREV_TARGET] = self.environment.saved_target_temp
            else:
                attributes[ATTR_PREV_TARGET] = self.environment.target_temp

        attributes[ATTR_HVAC_ACTION_REASON] = (
            self._hvac_action_reason or HVACActionReason.NONE
        )

        _LOGGER.debug("Extra state attributes: %s", attributes)

        return attributes

    def _set_support_flags(self) -> None:
        self.features.set_support_flags(
            self.presets.presets,
            self.presets.presets_range,
            self.presets.preset_mode,
            self.hvac_device.hvac_modes,
            self._hvac_mode,
        )
        self._attr_supported_features = self.features.supported_features
        _LOGGER.debug("Supported features: %s", self._attr_supported_features)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Call climate mode based on current mode."""
        _LOGGER.info("Setting hvac mode: %s", hvac_mode)

        if hvac_mode not in self.hvac_modes:
            _LOGGER.debug("Unrecognized hvac mode: %s", hvac_mode)
            return

        if hvac_mode == HVACMode.OFF:
            self._last_hvac_mode = self.hvac_device.hvac_mode
            _LOGGER.debug("Turning off with hvac mode: %s", self._last_hvac_mode)

        self._hvac_mode = hvac_mode
        self._set_support_flags()

        await self.hvac_device.async_set_hvac_mode(hvac_mode)

        self._hvac_action_reason = self.hvac_device.HVACActionReason

        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        temp_low = kwargs.get(ATTR_TARGET_TEMP_LOW)
        temp_high = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        _LOGGER.debug("Setting temperature: %s", temperature)
        _LOGGER.debug("Setting temperature low: %s", temp_low)
        _LOGGER.debug("Setting temperature high: %s", temp_high)

        temperatures = TargetTemperatures(temperature, temp_high, temp_low)

        if self.features.is_configured_for_heat_cool_mode:
            self._set_temperatures_dual_mode(temperatures)
        else:
            if temperature is None:
                return
            self.environment.set_temperature_target(temperature)
            self._target_temp = self.environment.target_temp

        await self._async_control_climate(force=True)
        self.async_write_ha_state()

    def _set_temperatures_dual_mode(self, temperatures: TargetTemperatures) -> None:
        """Set new target temperature for dual mode."""
        temperature = temperatures.temperature
        temp_low = temperatures.temp_low
        temp_high = temperatures.temp_high

        if self.features.is_target_mode:
            if temperature is None:
                return

            self.environment.set_temperature_range_from_hvac_mode(
                temperature, self.hvac_device.hvac_mode
            )

            self._target_temp = self.environment.target_temp
            self._target_temp_low = self.environment.target_temp_low
            self._target_temp_high = self.environment.target_temp_high

        elif self.features.is_range_mode:
            self.environment.set_temperature_range(temperature, temp_low, temp_high)
            self._target_temp = self.environment.target_temp
            self._target_temp_low = self.environment.target_temp_low
            self._target_temp_high = self.environment.target_temp_high

    async def _async_sensor_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self.environment.update_temp_from_state(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _async_sensor_floor_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle floor temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor floor change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self.environment.update_floor_temp_from_state(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _async_sensor_outside_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle outside temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor outside change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self.environment.update_outside_temp_from_state(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _async_sensor_humidity_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle outside temperature changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Sensor humidity change: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self.environment.update_floor_temp_from_state(new_state)
        await self._async_control_climate()
        self.async_write_ha_state()

    async def _check_device_initial_state(self) -> None:
        """Prevent the device from keep running if HVACMode.OFF."""
        _LOGGER.debug("Checking device initial state")
        if self._hvac_mode == HVACMode.OFF and self._is_device_active:
            _LOGGER.warning(
                "The climate mode is OFF, but the device is ON. Turning off device"
            )
            await self.hvac_device.async_turn_off()

    async def _async_opening_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle opening changes."""
        new_state = event.data.get("new_state")
        _LOGGER.info("Opening changed: %s", new_state)
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        opening_entity = event.data.get("entity_id")
        # get the opening timeout
        opening_timeout = None
        for opening in self.openings.openings:
            if opening_entity == opening[ATTR_ENTITY_ID]:
                opening_timeout = opening[ATTR_TIMEOUT]
                break

        # schedule the closing of the opening
        if opening_timeout is not None and (
            new_state.state == STATE_OPEN or new_state.state == STATE_ON
        ):
            _LOGGER.debug(
                "Scheduling state open of opening %s in %s",
                opening_entity,
                opening_timeout,
            )
            self.async_on_remove(
                async_call_later(
                    self.hass,
                    opening_timeout,
                    self._async_control_climate_forced,
                )
            )
        else:
            await self._async_control_climate(force=True)

        self.async_write_ha_state()

    async def _async_control_climate(self, time=None, force=False) -> None:
        """Control the climate device based on config."""

        _LOGGER.info("_async_control_climate, time %s, force %s", time, force)

        async with self._temp_lock:
            await self.hvac_device.async_control_hvac(time, force)
            _LOGGER.info(
                "updating HVACActionReason: %s", self.hvac_device.HVACActionReason
            )

            self._hvac_action_reason = self.hvac_device.HVACActionReason

    async def _async_control_climate_forced(self, time=None) -> None:
        _LOGGER.debug("_async_control_climate_forced, time %s", time)
        await self._async_control_climate(force=True, time=time)

    @callback
    def _async_hvac_mode_changed(self, hvac_mode) -> None:
        """Handle HVAC mode changes."""
        self.hvac_device.hvac_mode = hvac_mode
        self._set_support_flags()

        self.async_write_ha_state()

    @callback
    def _async_switch_changed(self, event: Event[EventStateChangedData]) -> None:
        """Handle heater switch state changes."""
        _LOGGER.debug("Switch changed: %s", event.data)
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if old_state is None:
            self.hass.create_task(self._check_device_initial_state())

        self.async_write_ha_state()

    @property
    def _is_device_active(self) -> bool:
        """If the toggleable device is currently active."""
        return self.hvac_device.is_active

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self.presets.set_preset_mode(preset_mode)

        self._attr_preset_mode = self.presets.preset_mode
        await self._async_control_climate(force=True)
        self.async_write_ha_state()

    @callback
    def _set_hvac_action_reason(self, *args) -> None:
        """My first service."""
        reason = args[0]
        _LOGGER.debug("Received HVACActionReasonExternal data %s", reason)

        # make sure its a valid reason
        if reason not in HVACActionReasonExternal:
            _LOGGER.error("Invalid HVACActionReasonExternal: %s", reason)
            return

        self._hvac_action_reason = reason

        self.schedule_update_ha_state(True)

    async def async_turn_on(self) -> None:
        """Turn on the device."""
        _LOGGER.debug("Turning on with last hvac mode: %s", self._last_hvac_mode)
        if self._last_hvac_mode is not None and self._last_hvac_mode != HVACMode.OFF:
            on_hvac_mode = self._last_hvac_mode
        else:
            device_hvac_modes_not_off = [
                mode for mode in self.hvac_device.hvac_modes if mode != HVACMode.OFF
            ]
            device_hvac_modes_not_off.sort()  # for sake of predictability and consistency

            # prioritize heat_cool mode if available
            if (
                HVACMode.HEAT_COOL in device_hvac_modes_not_off
                and device_hvac_modes_not_off.index(HVACMode.HEAT_COOL) != -1
            ):
                on_hvac_mode = HVACMode.HEAT_COOL
            else:
                on_hvac_mode = device_hvac_modes_not_off[0]

        _LOGGER.debug("Turning on with hvac mode: %s", on_hvac_mode)
        await self.async_set_hvac_mode(on_hvac_mode)

    async def async_turn_off(self) -> None:
        """Turn off the device."""
        await self.async_set_hvac_mode(HVACMode.OFF)
