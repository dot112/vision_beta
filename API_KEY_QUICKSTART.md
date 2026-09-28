# API Key Quick Start

This guide shows an administrator how to create a scoped API key, then how a client can use it to view a YOLO annotated camera stream.

## Before you start

- Set `BASE_URL` to the server address. The examples use `http://localhost:8000` for local development. Use HTTPS when connecting over a network.
- API key management requires a Level 3 Admin account and a dashboard JWT. API keys cannot create or manage other API keys.
- The camera must already be configured and connected to the server.

```powershell
$BaseUrl = "http://localhost:8000"
```

## API key permissions and endpoint reference

Select the scope that matches the endpoints the client needs. The key owner must meet the listed minimum clearance; when a key has multiple scopes, the owner must meet the highest minimum. Every API key also needs to be active, unexpired, and not revoked.

| Checkbox in the dashboard | Scope sent when creating the key | Minimum owner clearance | Endpoint access examples |
| --- | --- | --- | --- |
| Monitoring | `monitor:read` | Level 1 Operator | `GET /api/v1/cameras`; `GET /api/v1/cameras/{camera_id}/status`; `GET /api/v1/vision/stream/camera/{camera_id}`; `GET /api/v1/vision/annotated/camera/{camera_id}`; `GET /api/v1/qr/annotated/camera/{camera_id}`; read routes under `/api/v1/counting/`, `/api/v1/telemetry/`, and `/api/v1/plc/`; `GET /api/v1/mqtt/status` |
| Inspection history | `inspection:read` | Level 1 Operator | `GET /api/v1/vision/detections` |
| Run inspections | `inspection:trigger` | Level 2 Supervisor | Image and camera detection under `/api/v1/vision/`; QR decode and mobile scan under `/api/v1/qr/`; `/api/v1/control/trigger...` |
| Reset production counts | `production:reset` | Level 2 Supervisor | `POST /api/v1/counting/reset` |
| Read configuration | `configuration:read` | Level 1 Operator | `GET /api/v1/system/settings`; `GET /api/v1/system/endpoints`; `GET /api/v1/models`; `GET /api/v1/rules`; `GET /api/v1/actions`; `GET /api/v1/flows`; PLC action configuration reads; audit and change polling; `GET /api/v1/mqtt/certs` |
| Change configuration | `configuration:write` | Level 2 Supervisor | Camera create/update/delete; system settings and communication endpoint changes; model, rule, and counting configuration changes; other system configuration routes |
| Camera control | `camera:control` | Level 2 Supervisor | `POST /api/v1/cameras/{camera_id}/connect`; `POST /api/v1/cameras/{camera_id}/disconnect` |
| Configure PLC actions | `plc:configure` | Level 2 Supervisor | PLC action setup and endpoint changes under `/api/v1/plc/`; `POST /api/v1/plc/opcua/scan`; action configuration under `/api/v1/actions/` |
| Operate PLC actions | `plc:operate` | Level 2 Supervisor | `POST /api/v1/plc/actions/{id}/test`; `POST /api/v1/actions/{id}/execute`; PLC operation routes; flow tests also require this scope |
| Operate integrations | `integrations:operate` | Level 2 Supervisor | MQTT and `/api/v1/comms/` operations; `POST /api/v1/system/endpoints/{id}/test`; flow tests also require `plc:operate` |

API key creation, listing, revealing, and user administration are admin-only and cannot be done with an API key. Flow tests require several scopes together; consult the API's route policy before granting flow permissions. Requests that lack a required scope return `403`.

## 1. Sign in as an administrator

Send the admin username and password to `POST /api/v1/auth/login`:

```powershell
$LoginBody = @{ username = "admin"; password = "YOUR_ADMIN_PASSWORD" } | ConvertTo-Json
$Login = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/auth/login" `
  -ContentType "application/json" -Body $LoginBody
$AdminToken = $Login.access_token
$AdminHeaders = @{ Authorization = "Bearer $AdminToken" }
```

The commands save the returned `access_token` as `$AdminToken` for the next requests.

If the admin account already has an active session, the login may return `409 ACCOUNT_ALREADY_LOGGED_IN`. Sign out of that session, or use the dashboard's existing session.

## 2. Find the account that will own the key

API keys inherit their owner's clearance level. Use the user ID of the active account that should own this integration key:

```powershell
$Users = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/users" -Headers $AdminHeaders
$Users | Select-Object id, username, clearance_level, is_active
```

Copy the desired account's `id`. For a stream-only key, an Operator account (Level 1) is sufficient.

## 3. Create a stream-only key

Create the key with the `monitor:read` scope. This is the permission required for listing cameras and reading the annotated stream.

```powershell
$OwnerId = "OWNER_USER_ID"
$KeyBody = @{
  user_id = $OwnerId
  name = "YOLO stream client"
  scopes = @("monitor:read")
  expires_in_days = 90
} | ConvertTo-Json
$CreatedKey = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/auth/api-keys" `
  -Headers $AdminHeaders -ContentType "application/json" -Body $KeyBody
$ApiKey = $CreatedKey.api_key
```

Save the `api_key` from the response in a secure secret store. The key is also encrypted in the server database and can be revealed by an administrator from User / API Management. Never put it in a URL or commit it to source control.

```powershell
$ApiKeyHeaders = @{ "X-API-Key" = $ApiKey }
```

## 4. Find a camera ID

Use the API key in the `X-API-Key` header:

```powershell
$Cameras = Invoke-RestMethod -Uri "$BaseUrl/api/v1/cameras" -Headers $ApiKeyHeaders
$Cameras | Select-Object id, name, type, is_active
```

Copy the camera's `id` from the response:

```powershell
$CameraId = "CAMERA_ID_FROM_RESPONSE"
```

## 5. Open the YOLO annotated stream

The live stream endpoint is:

```http
GET /api/v1/vision/stream/camera/{camera_id}
```

It returns an MJPEG response (`multipart/x-mixed-replace; boundary=frame`) with YOLO annotations. For example:

```powershell
curl.exe -N "$BaseUrl/api/v1/vision/stream/camera/$CameraId" `
  -H "X-API-Key: $ApiKey"
```

The raw camera feed, without YOLO annotations, is:

```http
GET /api/v1/cameras/{camera_id}/stream
```

Use the same `X-API-Key` header. A browser's plain `<img src="...">` request cannot set this custom header; use an HTTP client that supports request headers or a trusted backend proxy.

## Optional: connect a camera through the API

If the camera is offline, a Supervisor can connect it in the dashboard. To connect it with an API key, create the key with both `monitor:read` and `camera:control`, and assign it to an account with at least Level 2 clearance. Then call:

```powershell
Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/cameras/$CameraId/connect" `
  -Headers $ApiKeyHeaders
```

## Common errors

- `401`: the key is invalid, expired, or revoked. Create a replacement key if needed.
- `403`: the key is missing `monitor:read`, or its owner does not meet the scope's clearance requirement.
- `404` from the stream endpoint: the camera ID is wrong, or the camera is offline or disconnected.

For interactive API exploration, open `/docs` when the server is running with `DEBUG=true`.
