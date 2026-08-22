"""Domain models for Govee Tap-to-Run / One-Click shortcuts.

One-Click (aka Tap-to-Run) is an account-level, multi-device automation
authored in the Govee app — distinct from the per-device ``lightScene``/
``diyScene`` capabilities ``select.py`` already exposes. It has no public
Developer API surface; the shape here is reverse-engineered from the
undocumented BFF endpoint (``GET /bff-app/v1/exec-plat/home``, see
``docs/govee-protocol-reference.md`` §4.3) and cross-checked against
govee2mqtt's implementation. Field names are best-effort until confirmed
against a live account response — see ``api/auth.py:fetch_one_clicks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OneClickRule:
    """A single device action within a One-Click, in wire-ready form.

    Exactly one of the native (``iot_cmd``/``iot_data``) or BLE
    (``ble_packets``) fields is populated — parsing normalizes the raw
    ``iotMsg``/``blueMsg`` rule entry into one shape so execution code
    doesn't need to branch on the source JSON.
    """

    device_id: str
    sku: str
    # Pre-resolved topic from the One-Click payload itself, if Govee included
    # one (``deviceObj.topic``). Falls back to the coordinator's own
    # device-topic cache when absent.
    topic: str | None = None

    # Native command replay (turn/brightness/colorwc/etc.) — same wire
    # format ``api/mqtt_control.py:command_to_mqtt`` produces for direct
    # device control.
    iot_cmd: str | None = None
    iot_data: dict | None = None
    cmd_version: int = 0

    # Raw BLE passthrough replay (``ptReal``) — same shape
    # ``GoveeAwsIotClient.async_publish_ptreal`` already accepts for
    # DreamView/DIY-scene/music-mode BLE commands.
    ble_packets: list[str] | None = None

    @property
    def is_native(self) -> bool:
        """True if this rule replays via the native cmd/data path."""
        return self.iot_cmd is not None

    @property
    def is_ble(self) -> bool:
        """True if this rule replays via the ptReal BLE-passthrough path."""
        return bool(self.ble_packets)


@dataclass(frozen=True)
class GoveeOneClick:
    """A user-defined Tap-to-Run / One-Click shortcut.

    ``rules`` is the flattened list of every device action the shortcut
    performs, in the order Govee returned them — activating a One-Click
    means replaying each rule to its target device.
    """

    id: str
    name: str
    rules: list[OneClickRule] = field(default_factory=list)
