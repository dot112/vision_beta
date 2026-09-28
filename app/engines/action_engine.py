from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx

from app.db.models.action import Action
from app.schemas.actions import ActionExecutionResult, ActionType
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ActionEngine:
    """
    Dispatcher for physical industrial outputs and digital triggers (Modbus PLC, MQTT, Webhooks).
    """
    _http_client: Optional[httpx.AsyncClient] = None

    @classmethod
    def _get_http_client(cls) -> httpx.AsyncClient:
        if cls._http_client is None or cls._http_client.is_closed:
            cls._http_client = httpx.AsyncClient(timeout=3.0, follow_redirects=False)
        return cls._http_client

    @classmethod
    async def close(cls) -> None:
        client = cls._http_client
        cls._http_client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    @classmethod
    async def execute(cls, action: Action, context: Dict[str, Any]) -> ActionExecutionResult:
        now = datetime.now(timezone.utc)
        act_type = str(action.action_type).lower() if action.action_type else "log"
        success = True
        msg = ""

        try:
            if not action.is_enabled:
                raise ValueError("Action is disabled")

            if act_type in (ActionType.LOG.value, "log"):
                msg = f"LOG ACTION [{action.name}]: Target={action.target} Context={context}"
                logger.info(msg)

            elif act_type in (ActionType.WEBHOOK.value, "webhook"):
                url = action.target
                from app.services.settings_persistence_service import SettingsPersistenceService
                allowed_urls = {
                    str(ep.get("url", "")).rstrip("/")
                    for ep in SettingsPersistenceService.get_endpoints(protocol="webhook")
                    if ep.get("enabled", True) and ep.get("url")
                }
                if not url.lower().startswith(("http://", "https://")) or url.rstrip("/") not in allowed_urls:
                    raise ValueError("Webhook action target must match an enabled configured webhook endpoint")
                payload = dict(action.payload or {})
                payload["context"] = context
                payload["timestamp"] = now.isoformat()

                client = cls._get_http_client()
                resp = await client.post(url, json=payload)
                success = resp.is_success
                msg = f"Webhook POST to configured endpoint returned status {resp.status_code}"

            elif act_type in (ActionType.MODBUS_COIL.value, "modbus_coil"):
                from app.services.modbus_service import ModbusService
                coil_idx = int(action.target)
                coil_val = bool(action.payload.get("value", True)) if action.payload else True
                ok, err = await ModbusService.write_coil(coil_idx, coil_val)
                success = ok
                msg = f"Modbus PLC: Wrote Coil {coil_idx} = {coil_val}" if ok else f"Modbus Coil Error: {err}"

            elif act_type in (ActionType.MODBUS_REGISTER.value, "modbus_register"):
                from app.services.modbus_service import ModbusService
                reg_idx = int(action.target)
                reg_val = int(action.payload.get("value", 1)) if action.payload else 1
                ok, err = await ModbusService.write_register(reg_idx, reg_val)
                success = ok
                msg = f"Modbus PLC: Wrote Holding Register {reg_idx} = {reg_val}" if ok else f"Modbus Register Error: {err}"

            elif act_type in (ActionType.MQTT_PUBLISH.value, "mqtt_publish"):
                from app.services.mqtt_service import MQTTService
                topic = action.target or "factory/actions"
                payload = dict(action.payload or {})
                payload["context"] = context
                payload["action_name"] = action.name
                payload["timestamp"] = now.isoformat()
                ok = await MQTTService.publish(topic, payload, qos=1)
                success = ok
                msg = f"MQTT: Published alert payload to topic '{topic}'" if ok else "MQTT Publish Failed"

            else:
                success = False
                msg = f"Unknown action type: {act_type}"

        except Exception as exc:
            success = False
            msg = f"Action execution failed: {type(exc).__name__}"
            logger.error("%s for action %s", msg, action.id)

        return ActionExecutionResult(
            action_id=str(action.id or ""),
            action_name=action.name or "Unnamed Action",
            success=success,
            message=msg,
            executed_at=now,
        )
