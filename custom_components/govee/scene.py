"""Scene platform for Govee integration.

Provides one HA scene entity per Tap-to-Run / One-Click shortcut — the
account-level, multi-device automations authored in the Govee app (govee2mqtt
calls this "click to run"). This is distinct from the per-device dynamic/DIY
scene dropdowns in select.py: those are single-device light effects
(`lightScene`/`diyScene` capabilities); One-Clicks can fan out to several
devices at once and have no per-device capability to attach to.

A prior scene.py existed for per-device dynamic scenes and was removed
2026-01-10 (one entity per scene produced 324 entities on a real account).
That concern doesn't apply here: One-Clicks are a handful of user-authored
shortcuts account-wide, not one entity per scene per device.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_ONE_CLICK, DEFAULT_ENABLE_ONE_CLICK
from .coordinator import GoveeCoordinator
from .models.one_click import GoveeOneClick

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Govee One-Click scene entities from a config entry."""
    coordinator: GoveeCoordinator = entry.runtime_data

    if not entry.options.get(CONF_ENABLE_ONE_CLICK, DEFAULT_ENABLE_ONE_CLICK):
        _LOGGER.debug("One-Click scene entities disabled")
        return

    entities = [
        GoveeOneClickSceneEntity(coordinator, entry, one_click)
        for one_click in coordinator.one_clicks.values()
    ]
    async_add_entities(entities)
    _LOGGER.debug("Set up %d Govee One-Click scene entities", len(entities))


class GoveeOneClickSceneEntity(CoordinatorEntity["GoveeCoordinator"], Scene):
    """A single Tap-to-Run / One-Click shortcut, exposed as a click-to-run scene.

    Unlike device platforms, this isn't attached to one GoveeDevice — a
    shortcut can target several. No device_info: with has_entity_name (Gold
    tier requirement, see quality_scale.yaml) a device grouping would prefix
    every entity with that device's name (e.g. "Govee Account Movie Night"),
    which is worse than the shortcut's own name shown plainly.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GoveeCoordinator,
        entry: ConfigEntry,
        one_click: GoveeOneClick,
    ) -> None:
        """Initialize the One-Click scene entity."""
        super().__init__(coordinator)
        self._one_click_id = one_click.id
        self._attr_name = one_click.name
        self._attr_unique_id = f"{entry.entry_id}_one_click_{one_click.id}"

    @property
    def available(self) -> bool:
        """Available only while MQTT is connected — there is no REST path."""
        return super().available and self.coordinator.mqtt_connected

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the One-Click shortcut."""
        one_click = self.coordinator.one_clicks.get(self._one_click_id)
        name = one_click.name if one_click else self._one_click_id

        success = await self.coordinator.async_execute_one_click(self._one_click_id)
        if success:
            _LOGGER.debug("Activated One-Click '%s'", name)
        else:
            _LOGGER.warning("Failed to activate One-Click '%s'", name)
