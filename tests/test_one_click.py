"""Tests for Tap-to-Run / One-Click support.

Covers:
- Parsing the undocumented ``exec-plat/home`` response into GoveeOneClick
  (api/auth.py:_parse_one_clicks) — native (iotMsg) and BLE (blueMsg) rules,
  unrecognized rules skipped, One-Clicks with zero executable rules dropped.
- fetch_one_clicks() HTTP-level contract (headers, auth failure, in-body
  BFF error envelope).
- GoveeCoordinator.async_execute_one_click — dispatch to the native vs BLE
  publish path, unresolvable-topic rules skipped without failing the run,
  MQTT-not-connected short-circuit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.govee.api.auth import (
    GoveeAuthClient,
    _parse_one_clicks,
)
from custom_components.govee.api.exceptions import GoveeAuthError
from custom_components.govee.coordinator import GoveeCoordinator
from custom_components.govee.models.one_click import GoveeOneClick, OneClickRule

# ==============================================================================
# HTTP mock helpers (self-contained; mirrors tests/test_auth.py's helpers)
# ==============================================================================


def make_mock_response(status: int, json_data: Any) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    return response


@asynccontextmanager
async def _async_cm(value: Any):
    yield value


def make_session_get(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.close = AsyncMock()
    session.get = lambda *a, **kw: _async_cm(response)
    return session


def _device_obj(device: str, sku: str, topic: str | None = None) -> dict[str, Any]:
    obj = {"device": device, "sku": sku}
    if topic is not None:
        obj["topic"] = topic
    return obj


def _one_click_payload(
    name: str,
    iot_rules: list[dict[str, Any]],
    preset_id: int | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "iotRules": iot_rules}
    if preset_id is not None:
        entry["presetId"] = preset_id
    return entry


# ==============================================================================
# _parse_one_clicks
# ==============================================================================


class TestParseOneClicks:
    """Parsing the exec-plat/home response body."""

    def test_native_iot_msg_rule_parsed(self):
        payload = {
            "data": {
                "oneClickComponents": [
                    {
                        "name": "Living Room",
                        "oneClicks": [
                            _one_click_payload(
                                "Movie Night",
                                [
                                    {
                                        "deviceObj": _device_obj(
                                            "AA:BB:CC:DD:EE:FF:00:11", "H6072"
                                        ),
                                        "rule": [
                                            {
                                                "iotMsg": {
                                                    "cmd": "turn",
                                                    "data": {"val": 1},
                                                    "cmdVersion": 0,
                                                }
                                            }
                                        ],
                                    }
                                ],
                                preset_id=42,
                            )
                        ],
                    }
                ]
            }
        }

        one_clicks = _parse_one_clicks(payload)

        assert len(one_clicks) == 1
        oc = one_clicks[0]
        assert oc.id == "42"
        assert oc.name == "Movie Night"
        assert len(oc.rules) == 1
        rule = oc.rules[0]
        assert rule.device_id == "AA:BB:CC:DD:EE:FF:00:11"
        assert rule.sku == "H6072"
        assert rule.is_native
        assert not rule.is_ble
        assert rule.iot_cmd == "turn"
        assert rule.iot_data == {"val": 1}

    def test_ble_blue_msg_rule_parsed(self):
        payload = {
            "data": {
                "oneClicks": [
                    _one_click_payload(
                        "DIY Fade",
                        [
                            {
                                "deviceObj": _device_obj(
                                    "11:22:33:44:55:66:77:88", "H6199"
                                ),
                                "rule": [{"blueMsg": "AQIDBAU="}],
                            }
                        ],
                    )
                ]
            }
        }

        one_clicks = _parse_one_clicks(payload)

        assert len(one_clicks) == 1
        rule = one_clicks[0].rules[0]
        assert rule.is_ble
        assert not rule.is_native
        assert rule.ble_packets == ["AQIDBAU="]

    def test_device_obj_topic_carried_through(self):
        payload = {
            "data": {
                "oneClicks": [
                    _one_click_payload(
                        "Bedtime",
                        [
                            {
                                "deviceObj": _device_obj(
                                    "AA:BB:CC:DD:EE:FF:00:11",
                                    "H6072",
                                    topic="GD/01/AAAA/rx",
                                ),
                                "rule": [
                                    {"iotMsg": {"cmd": "turn", "data": {"val": 0}}}
                                ],
                            }
                        ],
                    )
                ]
            }
        }

        one_clicks = _parse_one_clicks(payload)

        assert one_clicks[0].rules[0].topic == "GD/01/AAAA/rx"

    def test_rule_with_neither_iot_msg_nor_blue_msg_is_skipped(self):
        payload = {
            "data": {
                "oneClicks": [
                    _one_click_payload(
                        "Unsupported",
                        [
                            {
                                "deviceObj": _device_obj(
                                    "AA:BB:CC:DD:EE:FF:00:11", "H6072"
                                ),
                                "rule": [{"cmdType": 3, "cmdVal": {"scenesCode": 7}}],
                            }
                        ],
                    )
                ]
            }
        }

        one_clicks = _parse_one_clicks(payload)

        # The One-Click has zero executable rules once the unsupported entry
        # is dropped, so it must not be surfaced as a dead scene.
        assert one_clicks == []

    def test_one_click_with_no_rules_at_all_is_dropped(self):
        payload = {"data": {"oneClicks": [{"name": "Empty", "iotRules": []}]}}

        assert _parse_one_clicks(payload) == []

    def test_missing_data_key_returns_empty_list(self):
        assert _parse_one_clicks({"status": 200, "message": "ok"}) == []
        assert _parse_one_clicks(None) == []

    def test_duplicate_names_get_distinct_ids(self):
        rule = {
            "deviceObj": _device_obj("AA:BB:CC:DD:EE:FF:00:11", "H6072"),
            "rule": [{"iotMsg": {"cmd": "turn", "data": {"val": 1}}}],
        }
        payload = {
            "data": {
                "oneClicks": [
                    _one_click_payload("Movie Night", [rule]),
                    _one_click_payload("Movie Night", [rule]),
                ]
            }
        }

        one_clicks = _parse_one_clicks(payload)

        assert len(one_clicks) == 2
        assert one_clicks[0].id != one_clicks[1].id


# ==============================================================================
# GoveeAuthClient.fetch_one_clicks
# ==============================================================================


class TestFetchOneClicks:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_list(self):
        body = {
            "status": 200,
            "data": {
                "oneClicks": [
                    _one_click_payload(
                        "Movie Night",
                        [
                            {
                                "deviceObj": _device_obj(
                                    "AA:BB:CC:DD:EE:FF:00:11", "H6072"
                                ),
                                "rule": [
                                    {"iotMsg": {"cmd": "turn", "data": {"val": 1}}}
                                ],
                            }
                        ],
                    )
                ]
            },
        }
        session = make_session_get(make_mock_response(200, body))
        client = GoveeAuthClient(session=session)

        result = await client.fetch_one_clicks(token="tok")

        assert len(result) == 1
        assert result[0].name == "Movie Night"

    @pytest.mark.asyncio
    async def test_http_401_raises_auth_error(self):
        session = make_session_get(make_mock_response(401, {"message": "Unauthorized"}))
        client = GoveeAuthClient(session=session)

        with pytest.raises(GoveeAuthError):
            await client.fetch_one_clicks(token="expired")

    @pytest.mark.asyncio
    async def test_in_body_401_raises_auth_error(self):
        """Govee's BFF can answer HTTP 200 with an in-body 401 (issue #132 pattern)."""
        session = make_session_get(
            make_mock_response(200, {"status": 401, "message": "token rejected"})
        )
        client = GoveeAuthClient(session=session)

        with pytest.raises(GoveeAuthError):
            await client.fetch_one_clicks(token="expired")


# ==============================================================================
# GoveeCoordinator.async_execute_one_click
# ==============================================================================


class TestAsyncExecuteOneClick:
    """Activation replays each rule via the same publish primitives used
    elsewhere (native cmd/data, or ptReal for BLE) — see coordinator.py."""

    def _make_coordinator(self) -> GoveeCoordinator:
        coord = object.__new__(GoveeCoordinator)
        coord._one_clicks = {}
        coord._device_topics = {}
        coord._iot_credentials = None
        coord._mqtt_client = MagicMock(connected=True)
        coord._mqtt_client.async_publish_command = AsyncMock(return_value=True)
        coord._mqtt_client.async_publish_ptreal = AsyncMock(return_value=True)
        return coord

    @pytest.mark.asyncio
    async def test_unknown_one_click_returns_false(self):
        coord = self._make_coordinator()

        result = await coord.async_execute_one_click("does-not-exist")

        assert result is False

    @pytest.mark.asyncio
    async def test_mqtt_not_connected_returns_false(self):
        coord = self._make_coordinator()
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="Movie",
            rules=[
                OneClickRule(
                    device_id="AA:BB:CC:DD:EE:FF:00:11",
                    sku="H6072",
                    topic="GD/x",
                    iot_cmd="turn",
                    iot_data={"val": 1},
                )
            ],
        )
        coord._mqtt_client = None

        result = await coord.async_execute_one_click("mc")

        assert result is False

    @pytest.mark.asyncio
    async def test_native_rule_publishes_command(self):
        coord = self._make_coordinator()
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="Movie",
            rules=[
                OneClickRule(
                    device_id="AA:BB:CC:DD:EE:FF:00:11",
                    sku="H6072",
                    topic="GD/x",
                    iot_cmd="turn",
                    iot_data={"val": 1},
                    cmd_version=0,
                )
            ],
        )

        result = await coord.async_execute_one_click("mc")

        assert result is True
        coord._mqtt_client.async_publish_command.assert_awaited_once_with(
            "GD/x", "turn", {"val": 1}, cmd_version=0
        )
        coord._mqtt_client.async_publish_ptreal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ble_rule_publishes_ptreal(self):
        coord = self._make_coordinator()
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="DIY Fade",
            rules=[
                OneClickRule(
                    device_id="11:22:33:44:55:66:77:88",
                    sku="H6199",
                    topic="GD/y",
                    ble_packets=["AQIDBAU="],
                )
            ],
        )

        result = await coord.async_execute_one_click("mc")

        assert result is True
        coord._mqtt_client.async_publish_ptreal.assert_awaited_once_with(
            "11:22:33:44:55:66:77:88", "H6199", ["AQIDBAU="], "GD/y"
        )
        coord._mqtt_client.async_publish_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_device_topic_cache_when_rule_has_none(self):
        coord = self._make_coordinator()
        coord._device_topics["AA:BB:CC:DD:EE:FF:00:11"] = "GD/from-cache"
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="Movie",
            rules=[
                OneClickRule(
                    device_id="AA:BB:CC:DD:EE:FF:00:11",
                    sku="H6072",
                    topic=None,
                    iot_cmd="turn",
                    iot_data={"val": 1},
                )
            ],
        )

        result = await coord.async_execute_one_click("mc")

        assert result is True
        coord._mqtt_client.async_publish_command.assert_awaited_once_with(
            "GD/from-cache", "turn", {"val": 1}, cmd_version=0
        )

    @pytest.mark.asyncio
    async def test_unresolvable_topic_is_skipped_not_fatal(self):
        """A rule whose device has no known topic (e.g. a group/scenic target,
        upstream govee2mqtt issue #406) is skipped; other rules still run."""
        coord = self._make_coordinator()
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="Movie",
            rules=[
                OneClickRule(
                    device_id="UNRESOLVABLE",
                    sku="GROUP",
                    topic=None,
                    iot_cmd="turn",
                    iot_data={"val": 1},
                ),
                OneClickRule(
                    device_id="AA:BB:CC:DD:EE:FF:00:11",
                    sku="H6072",
                    topic="GD/x",
                    iot_cmd="brightness",
                    iot_data={"val": 50},
                ),
            ],
        )

        result = await coord.async_execute_one_click("mc")

        # One rule was unresolvable, but the other still ran -> overall success.
        assert result is True
        coord._mqtt_client.async_publish_command.assert_awaited_once_with(
            "GD/x", "brightness", {"val": 50}, cmd_version=0
        )

    @pytest.mark.asyncio
    async def test_all_rules_unresolvable_returns_false(self):
        coord = self._make_coordinator()
        coord._one_clicks["mc"] = GoveeOneClick(
            id="mc",
            name="Movie",
            rules=[
                OneClickRule(
                    device_id="UNRESOLVABLE",
                    sku="GROUP",
                    topic=None,
                    iot_cmd="turn",
                    iot_data={"val": 1},
                )
            ],
        )

        result = await coord.async_execute_one_click("mc")

        assert result is False
        coord._mqtt_client.async_publish_command.assert_not_awaited()
