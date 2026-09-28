from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class MQTTConnectRequest(BaseModel):
    host: str = Field(..., description="EMQX broker IP or hostname (e.g. 192.168.1.10)")
    port: int = Field(default=1883, ge=1, le=65535, description="Port: 1883=plain, 8883=TLS, 8084=WSS")
    username: Optional[str] = Field(default=None, description="EMQX username")
    password: Optional[str] = Field(default=None, description="EMQX password")
    client_id: str = Field(default="industrial_vision_api", description="Unique MQTT client ID")
    tls_enabled: bool = Field(default=False, description="Enable TLS/mTLS")
    ca_cert_filename: Optional[str] = Field(default=None, description="CA cert file previously uploaded (e.g. ca.crt)")
    client_cert_filename: Optional[str] = Field(default=None, description="Client cert file previously uploaded (e.g. client.crt)")
    client_key_filename: Optional[str] = Field(default=None, description="Private key file previously uploaded (e.g. client.key)")


class MQTTPublishRequest(BaseModel):
    topic: str = Field(..., description="MQTT topic path")
    payload: Dict[str, Any] = Field(..., description="JSON payload")
    qos: int = Field(default=0, ge=0, le=2)
    retain: bool = Field(default=False)


class MQTTSubscribeRequest(BaseModel):
    topic: str = Field(..., description="Topic to subscribe to (supports # and + wildcards)")


class MQTTStatusResponse(BaseModel):
    broker_host: str
    broker_port: int
    is_connected: bool
    client_id: str
    tls_enabled: bool
    ca_cert: Optional[str] = None
    client_cert: Optional[str] = None
    subscriptions: List[str] = []


class MQTTCertListResponse(BaseModel):
    ca_certs: List[str]
    client_certs: List[str]
    private_keys: List[str]
    all_files: List[str] = []
