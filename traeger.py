# Version: 1.0.13-grok-is-a-fuckup
# Revision: 2025-08-19 00:39:00
"""
Traeger API client for Home Assistant.
"""

import asyncio
import datetime
import json
import logging
import ssl
import time
import urllib.parse
import async_timeout

from datetime import timedelta
from typing import Any, Optional
from collections.abc import Callable
import aiohttp
from aiomqtt import Client as MQTTClient, MqttError
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

CLIENT_ID = "2fuohjtqv1e63dckp5v84rau0j"
TIMEOUT = 60
API_BASE = "https://1ywgyc65d1.execute-api.us-west-2.amazonaws.com/prod"


class traeger:
    def __init__(self, username, password, hass: HomeAssistant, session):
        self.username = username
        self.password = password
        self.hass = hass
        self.session = session
        self.mqtt_client = None
        self.grills = []
        self.grill_status = {}
        self.access_token = None
        self.token = None
        self.token_expires = 0
        self.mqtt_url = None
        self.mqtt_url_expires = time.time()
        self.grill_callbacks = {}
        self._mqtt_connected = False
        self._last_callback_time = {}
        self._initial_setup = True
        self._last_syncmain_time = 0
        self.task = None
        self.message_task = None
        self.coordinator = None  # type: Optional[object]
        self._coordinator: Optional[Any] = None
        self._poke_last: dict[str, float] = {}
        self._poke_backoff: dict[str, float] = {}
        self._last_mqtt_rx: dict[str, float] = {}
        self._auto_poke_unsub: dict[str, Callable[[], None]] = {}

    def token_remaining(self):
        return self.token_expires - time.time()

    async def api_wrapper(
        self, method: str, url: str, data: dict = {}, headers: dict = {}
    ):
        _LOGGER.debug(
            f"API call: method={method}, url={url}, data={data}, headers={headers}"
        )
        try:
            async with async_timeout.timeout(TIMEOUT):
                if method == "get":
                    response = await self.session.get(url, headers=headers)
                elif method == "post":
                    response = await self.session.post(url, json=data, headers=headers)
                elif method == "post_raw":
                    response = await self.session.post(
                        url, data=json.dumps(data), headers=headers
                    )
                else:
                    raise ValueError(f"Unsupported method: {method}")
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if "application/x-amz-json-1.1" in content_type:
                    return json.loads(await response.text())
                return await response.json()
        except asyncio.TimeoutError:
            _LOGGER.error(f"Timeout error fetching data from {url}")
            raise
        except aiohttp.ClientResponseError as e:
            _LOGGER.error(
                f"Client error fetching data from {url}: {e.status}, message={e.message}, url={url}, body={await response.text()}"
            )
            raise
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Client error fetching data from {url}: {e}")
            raise

    async def do_cognito(self):
        t = datetime.datetime.utcnow()
        amzdate = t.strftime("%Y%m%dT%H%M%SZ")
        payload = {
            "AuthParameters": {"USERNAME": self.username, "PASSWORD": self.password},
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": CLIENT_ID,
        }
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Date": amzdate,
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        }
        _LOGGER.debug(f"Sending Cognito request: payload={payload}, headers={headers}")
        try:
            response = await self.api_wrapper(
                "post",
                "https://cognito-idp.us-west-2.amazonaws.com/",
                data=payload,
                headers=headers,
            )
            _LOGGER.debug(f"Cognito response: {response}")
            return response
        except aiohttp.ClientResponseError as e:
            _LOGGER.error(
                f"Cognito request failed: status={e.status}, message={e.message}, response={e}"
            )
            raise
        except Exception as e:
            _LOGGER.error(f"Cognito request failed: {e}")
            raise

    async def refresh_token(self):
        if self.token_remaining() < 60:
            request_time = time.time()
            response = await self.do_cognito()
            self.token_expires = (
                response["AuthenticationResult"]["ExpiresIn"] + request_time
            )
            self.token = response["AuthenticationResult"]["IdToken"]
            _LOGGER.debug(
                f"Refreshed token: token={self.token[:10]}..., expires={self.token_expires}"
            )

    async def get_user_data(self):
        await self.refresh_token()
        return await self.api_wrapper(
            "get",
            "https://1ywgyc65d1.execute-api.us-west-2.amazonaws.com/prod/users/self",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    async def send_command(self, grill_id: str, command: str) -> None:
        """Send a single command token as {'command': '<token>'} with bearer auth."""
        await self.refresh_token()
        url = f"{API_BASE}/things/{grill_id}/commands"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        _LOGGER.debug(f"Send Command Topic: {grill_id}, Send Command: {command}")
        await self.api_wrapper(
            "post_raw", url, data={"command": command}, headers=headers
        )

    async def _on_mqtt_message(self, grill_id: str, payload: dict) -> None:
        """Handle incoming MQTT and push to coordinator."""
        try:
            if topic.startswith("prod/thing/update/"):
                grill_id = topic.split("/")[-1]
                state = json.loads(payload.decode("utf-8"))
                self.grill_status[grill_id] = state
                # Push snapshot so coordinator consumers refresh
                if self._coordinator is not None:
                    # Update your existing client cache
                    self._coordinator.async_set_updated_data(dict(self.grill_status))
        except Exception:
            _LOGGER.exception("Failed to process MQTT update")
        self._last_mqtt_rx[grill_id] = time.monotonic()

    async def trigger_mqtt_refresh(
        self,
        grill_id: str,
        *,
        min_interval: float = 10.0,  # floor between sends when healthy
        max_backoff: float = 60.0,  # cap for exponential backoff on errors
        start_backoff: float = 10.0,  # initial backoff when first failure happens
    ) -> None:
        """Send command 90 to prompt MQTT updates; rate-limited with backoff."""
        now = time.monotonic()
        last = getattr(self, "_poke_last", {}).get(grill_id, 0.0)
        backoff = getattr(self, "_poke_backoff", {}).get(grill_id, 0.0)
        window = max(min_interval, backoff)
        if now - last < window:
            _LOGGER.debug(
                f"Skipping MQTT poke for {grill_id} (window {window:.1f}s, last {now - last:.1f}s ago)"
            )
            return
        _LOGGER.debug(f"Poking MQTT for grill with command 90 (grill_id={grill_id})")
        try:
            await self.send_command(grill_id, "90")
            self._poke_last[grill_id] = now
            self._poke_backoff[grill_id] = 0.0
        except Exception as err:
            next_backoff = min(
                max(start_backoff, backoff * 2 or start_backoff), max_backoff
            )
            self._poke_backoff[grill_id] = next_backoff
            self._poke_last[grill_id] = now
            _LOGGER.debug(
                f"MQTT poke failed for {grill_id} ({err}); backing off {next_backoff:.1f}s"
            )

    def schedule_auto_poke(
        self,
        hass: HomeAssistant,
        grill_id: str,
        *,
        interval_seconds: int = 15,
        idle_threshold_seconds: int = 30,
    ) -> None:
        """Auto-poke with command 90 if MQTT has been idle longer than threshold."""
        if grill_id in self._auto_poke_unsub:
            return

        _LOGGER.debug(
            f"Scheduling auto-poke for {grill_id} every {interval_seconds}s (idle>{idle_threshold_seconds}s)"
        )

        async def _tick(now: datetime.datetime) -> None:
            """Run in event loop; safe to await."""
            last_rx = self._last_mqtt_rx.get(grill_id, 0.0)
            if last_rx == 0.0:
                return
            idle = time.monotonic() - last_rx
            if idle >= idle_threshold_seconds:
                _LOGGER.debug(
                    f"Auto-poke: MQTT idle {idle:.1f}s for {grill_id}, sending 90"
                )
                await self.trigger_mqtt_refresh(grill_id)

        unsub = async_track_time_interval(
            hass, _tick, timedelta(seconds=interval_seconds)
        )
        self._auto_poke_unsub[grill_id] = unsub

    def cancel_auto_poke(self, grill_id: str) -> None:
        """Cancel the auto-poke scheduler for a grill."""
        if unsub := self._auto_poke_unsub.pop(grill_id, None):
            try:
                unsub()
            except Exception:
                pass

    # Optional: keep a convenience that accepts int too
    async def send_command_int(self, grill_id: str, command: int) -> None:
        await self.send_command(grill_id, str(command))

    async def update_state(self, thingName):
        await self.send_command(thingName, "90")

    async def set_temperature(self, thingName, temp):
        await self.send_command(thingName, f"11,{temp}")

    async def set_probe_temperature(self, thingName, temp):
        await self.send_command(thingName, f"14,{temp}")

    async def set_switch(self, thingName, switchval):
        await self.send_command(thingName, str(switchval))

    async def shutdown_grill(self, thingName):
        await self.send_command(thingName, "17")

    async def set_timer_sec(self, thingName, time_s):
        await self.send_command(thingName, f"12,{time_s}")

    async def set_custom_cook(self, thingName, slot_num):
        _LOGGER.debug(f"Setting custom cook cycle for {thingName}: slot {slot_num}")
        await self.send_command(thingName, f"15,{slot_num}")

    async def update_grills(self):
        json_data = await self.get_user_data()
        self.grills = json_data["things"]
        _LOGGER.debug(f"Updated grills: {self.grills}")

    def get_grills(self):
        return self.grills

    def set_callback_for_grill(self, grill_id, callback):
        if grill_id not in self.grill_callbacks:
            self.grill_callbacks[grill_id] = []
        if callback is not None and callable(callback):
            self.grill_callbacks[grill_id].append(callback)
            _LOGGER.debug(
                f"Registered callback for grill {grill_id}: {callback.__qualname__} from {getattr(callback, '__self__', 'Unknown')}"
            )
        else:
            _LOGGER.error(f"Invalid callback for grill {grill_id}: {callback}")

    def mqtt_url_remaining(self):
        return self.mqtt_url_expires - time.time()

    async def refresh_mqtt_url(self):
        await self.refresh_token()
        if self.mqtt_url_remaining() < 60:
            try:
                mqtt_request_time = time.time()
                json_data = await self.api_wrapper(
                    "post",
                    "https://1ywgyc65d1.execute-api.us-west-2.amazonaws.com/prod/mqtt-connections",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.mqtt_url_expires = (
                    json_data["expirationSeconds"] + mqtt_request_time
                )
                self.mqtt_url = json_data["signedUrl"]
                _LOGGER.debug(
                    f"MQTT URL refreshed: {self.mqtt_url}, expires={self.mqtt_url_expires}"
                )
            except Exception as e:
                _LOGGER.error(f"Failed to refresh MQTT URL: {e}")
                raise

    async def connect_mqtt(self):
        if self._mqtt_connected:
            _LOGGER.debug("MQTT already connected, skipping")
            return
        try:
            async with async_timeout.timeout(TIMEOUT):
                await self.refresh_mqtt_url()
                mqtt_parts = urllib.parse.urlparse(self.mqtt_url)
                headers = {"Host": mqtt_parts.netloc}
                ws_path = f"{mqtt_parts.path}?{mqtt_parts.query}"
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                self.mqtt_client = MQTTClient(
                    hostname=mqtt_parts.hostname,
                    port=443,
                    keepalive=300,
                    transport="websockets",
                    websocket_path=ws_path,
                    websocket_headers=headers,
                    tls_context=context,
                )
                await self.mqtt_client.__aenter__()
                self._mqtt_connected = True
                _LOGGER.info("Grill Connected")
                for grill in self.grills:
                    grill_id = grill["thingName"]
                    await self.mqtt_client.subscribe(
                        f"prod/thing/update/{grill_id}", qos=1
                    )
                    await self.update_state(grill_id)
                self.message_task = self.hass.async_create_task(self.process_messages())
        except (asyncio.TimeoutError, aiohttp.ClientError, MqttError) as e:
            _LOGGER.error(f"Failed to connect MQTT: {e}")
            self.mqtt_client = None
            self._mqtt_connected = False
            raise

    def attach_coordinator(self, coordinator) -> None:
        """Attach HA coordinator so we can push snapshots on each MQTT update."""
        self._coordinator = coordinator
        self.coordinator = coordinator  # keep legacy attribute in sync

    async def _handle_mqtt_update(self, topic: str, payload: bytes) -> None:
        """Process an MQTT update and push to coordinator."""
        try:
            if topic.startswith("prod/thing/update/"):
                grill_id = topic.split("/")[-1]
                state = json.loads(payload.decode("utf-8"))
                self.grill_status[grill_id] = state
                # Push snapshot so coordinator consumers refresh
                if self._coordinator is not None:
                    # Update your existing client cache
                    self._coordinator.async_set_updated_data(dict(self.grill_status))
        except Exception:
            _LOGGER.exception("Failed to process MQTT update")

    async def process_messages(self):
        """Your existing MQTT loop; call _handle_mqtt_update per message."""
        try:
            async for message in self.mqtt_client.messages:
                topic = str(message.topic)
                if isinstance(message.payload, bytes):
                    payload = message.payload.decode("utf-8")
                else:
                    payload = message.payload
                _LOGGER.debug(f"grill_message: topic={topic}, payload={payload}")
                _LOGGER.info(
                    f"Token Time Remaining: {self.token_remaining()} MQTT Time Remaining: {self.mqtt_url_remaining()}"
                )

                if topic.startswith("prod/thing/update/"):
                    grill_id = topic[len("prod/thing/update/") :]
                    self.grill_status[grill_id] = json.loads(payload)

                    # Update idle tracker so schedule_auto_poke knows MQTT is flowing
                    self._last_mqtt_rx[grill_id] = time.monotonic()
                    _LOGGER.debug(
                        f"Updated grill_status for {grill_id}: keys={list(self.grill_status[grill_id].keys())}"
                    )
                    _LOGGER.debug(
                        f"MQTT RX timestamp updated for {grill_id}: {self._last_mqtt_rx[grill_id]:.3f}"
                    )

                    # Always push latest snapshot to coordinator
                    if self._coordinator is not None:
                        self._coordinator.async_set_updated_data(
                            dict(self.grill_status)
                        )

                    # Keep old behavior: skip entity callbacks only on very first packet
                    if self._initial_setup:
                        self._initial_setup = False
                        continue
                    if grill_id in self.grill_callbacks:
                        current_time = time.time()
                        last_time = self._last_callback_time.get(grill_id, 0)
                        if current_time - last_time >= 15:
                            for callback in self.grill_callbacks[grill_id]:
                                if callback is not None and callable(callback):
                                    _LOGGER.debug(
                                        f"Executing callback for grill {grill_id}: {callback.__qualname__} from {getattr(callback, '__self__', 'Unknown')}"
                                    )
                                    self.hass.async_create_task(callback())
                                else:
                                    _LOGGER.error(
                                        f"Skipping invalid callback for grill {grill_id}: {callback}"
                                    )
                            self._last_callback_time[grill_id] = current_time
        except MqttError as err:
            _LOGGER.error(f"MQTT Error: {err}")
            self._mqtt_connected = False
            self.hass.loop.call_later(30, self.syncmain)
        except asyncio.CancelledError:
            _LOGGER.debug("Message task cancelled")
        except Exception as e:
            _LOGGER.error(f"Error in message loop: {e}")
            self._mqtt_connected = False
            self.hass.loop.call_later(30, self.syncmain)
        finally:
            if self.mqtt_client is not None:
                await self.mqtt_client.__aexit__(None, None, None)
                self.mqtt_client = None
                self._mqtt_connected = False

    def syncmain(self):
        current_time = time.time()
        if current_time - self._last_syncmain_time < 30:
            _LOGGER.debug(f"Skipping syncmain, too soon since last call")
            return
        self._last_syncmain_time = current_time
        _LOGGER.debug("Call_Later SyncMain CreatingTask for async Main.")
        self.hass.create_task(self.main())

    async def main(self):
        _LOGGER.debug(f"Current Main Loop Time: {time.time()}")
        _LOGGER.debug(
            f"MQTT Logger Token Time Remaining: {self.token_remaining()} MQTT Time Remaining: {self.mqtt_url_remaining()}"
        )
        if self.mqtt_url_remaining() < 60 or not self._mqtt_connected:
            if self.message_task is not None:
                self.message_task.cancel()
                try:
                    await self.message_task
                except asyncio.CancelledError:
                    pass
                self.message_task = None
            self.mqtt_client = None
            self._mqtt_connected = False
            await self.connect_mqtt()
        self.task = self.hass.loop.call_later(30, self.syncmain)

    def get_state_for_device(self, thingName):
        state = self.grill_status.get(thingName, {})
        _LOGGER.debug(f"get_state_for_device {thingName}: keys={list(state.keys())}")
        return state

    def get_details_for_device(self, thingName):
        return self.grill_status.get(thingName, {}).get("details")

    def get_limits_for_device(self, thingName):
        return self.grill_status.get(thingName, {}).get("limits")

    def get_settings_for_device(self, thingName):
        return self.grill_status.get(thingName, {}).get("settings")

    def get_features_for_device(self, thingName):
        return self.grill_status.get(thingName, {}).get("features")

    def get_cloudconnect(self, thingName):
        return (
            self._mqtt_connected
            and thingName in self.grill_status
            and self.grill_status[thingName].get("status", {}).get("connected", False)
        )

    def get_units_for_device(self, thingName):
        state = self.grill_status.get(thingName, {}).get("status", {})
        if not state:
            return UnitOfTemperature.FAHRENHEIT
        return (
            UnitOfTemperature.CELSIUS
            if state.get("units", 0) == 0
            else UnitOfTemperature.FAHRENHEIT
        )

    def get_details_for_accessory(self, thingName, accessory_id):
        state = self.grill_status.get(thingName, {}).get("status", {})
        if not state:
            return None
        for accessory in state.get("acc", []):
            if accessory["uuid"] == accessory_id:
                return accessory
        return None

    async def start(self):
        try:
            # Top-level timeout removed to avoid loop access before scheduling
            await self.update_grills()
            for grill in self.grills:
                grill_id = grill["thingName"]
                await self.update_state(grill_id)
            await self.connect_mqtt()
        except (asyncio.TimeoutError, aiohttp.ClientError, MqttError) as e:
            _LOGGER.error(f"Failed to start Traeger: {e}")
            raise
        _LOGGER.debug("Traeger setup completed")
        self.task = self.hass.loop.call_later(30, self.syncmain)

    async def kill(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None
        if self.message_task is not None:
            self.message_task.cancel()
            try:
                await self.message_task
            except asyncio.CancelledError:
                pass
            self.message_task = None
        if self.mqtt_client is not None:
            await self.mqtt_client.__aexit__(None, None, None)
            self.mqtt_client = None
            self._mqtt_connected = False
        self.mqtt_url_expires = time.time()
        self._initial_setup = True
        for grill in self.grills:
            grill_id = grill["thingName"]
            if grill_id in self.grill_callbacks:
                for callback in self.grill_callbacks[grill_id]:
                    if callback is not None and callable(callback):
                        _LOGGER.debug(
                            f"Executing callback on kill for grill {grill_id}: {callback.__qualname__} from {getattr(callback, '__self__', 'Unknown')}"
                        )
                        self.hass.async_create_task(callback())
                    else:
                        _LOGGER.error(
                            f"Skipping invalid callback on kill for grill {grill_id}: {callback}"
                        )
