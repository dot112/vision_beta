# Sparkplug B edge node and bundled broker: design

Date: 2026-10-06. Status: awaiting review.

## Goal

Ignition (with the Cirrus Link MQTT Engine module), or any other Sparkplug B
host, reads every production line of this server as tags that appear by
themselves, knows within seconds when the server is offline, and can start or
stop a line and reset its counters when that is allowed. A site with no MQTT
broker gets one from the same `docker compose` command.

Agreed with the owner on 2026-10-06:

- The server is a Sparkplug **edge node**. It is not a host application.
- Commands: rebirth always; start/stop a line and reset its counters only when
  the channel's "Allow commands" switch is on (off by default).
- The broker is a bundled Mosquitto service, not a broker inside the Python
  process.

## Not included

- Host application role (reading tags that other devices publish).
- Buffering messages while the broker is unreachable (store and forward).
- PLC outputs as commands.
- TLS on the bundled broker. A channel can already use TLS to any broker.
- Sparkplug templates, datasets and metric aliases.

## Where it is set up

Sparkplug is a setting of an MQTT channel (Connections). A channel is already
the broker's address, login and TLS; these fields are added to its record:

| Field | Type | Default | Rule |
|---|---|---|---|
| `sparkplug_enabled` | bool | false | |
| `sparkplug_group_id` | text | "" | required when enabled; 1 to 128 characters; none of `/ + #` |
| `sparkplug_node_id` | text | "" | same rule |
| `sparkplug_host_id` | text | "" | optional; same characters |
| `sparkplug_allow_commands` | bool | false | |
| `sparkplug_interval_ms` | int | 1000 | 100 to 60000 |

Two enabled channels with the same broker address, group ID and node ID are
refused: they would be one edge node connected twice.

No settings upgrade step is needed: a channel saved before this has none of
the fields, which reads as Sparkplug off.

The Sparkplug connection is separate from the channel's connection for send
cards. It uses the channel's address, login, TLS and MQTT version, and the
client ID `<channel client id>-spb`. Send cards on the same channel are not
affected. The channel's QoS, retain and birth/close/will settings do not apply
to Sparkplug: the specification fixes them.

## What is published

Topics follow Sparkplug B 3.0: `spBv1.0/<group>/<type>/<node>[/<device>]`.

The **edge node** is the server. Its metrics:

| Metric | Type | Notes |
|---|---|---|
| `bdSeq` | Int64 | the birth/death sequence number |
| `Node Control/Rebirth` | Boolean | always false; a host writes true to ask for a rebirth |
| `Node Info/Software Version` | String | `APP_VERSION` |

Each **production line** is a device. Its device ID is the line's name with
`/`, `+` and `#` replaced by `_`, at most 128 characters; a line with no
usable name uses its line id, and two lines whose names give the same ID get
the line id appended to the second. A metric name is never listed twice: a
second camera or class with a name already used gets a suffix. Its metrics:

| Metric | Type | Source |
|---|---|---|
| `Line/Running` | Boolean | the line is started |
| `Line/Name` | String | |
| `Counts/Inspected`, `Counts/Good`, `Counts/Rejected` | Int64 | the line's totals |
| `Counts/Class/<class>` | Int64 | one per class: every expected and defect class of the line's vision cameras, plus any class already counted |
| `Rate/Products Per Minute` | Double | rounded to 2 decimals |
| `Quality/Yield Percent`, `Quality/Defect PPM` | Double | rounded to 2 decimals |
| `Last Product/Result` | String | `PASSED` or `REJECTED`; empty until the first product |
| `Last Product/Class`, `Last Product/Reject Reason`, `Last Product/Code` | String | |
| `Last Product/Confidence` | Double | |
| `Last Code/Text`, `Last Code/Status` | String | the latest code read: `known`, `unknown` or `no_read` |
| `Cameras/<camera name>/Connected` | Boolean | one per camera of the line |
| `Alarms/Active Count` | Int32 | active alarms of this line |
| `Alarms/Active` | String | their codes, sorted, comma separated |
| `Commands/Reset Counters` | Boolean | always false; a host writes true |

Every metric carries its name, datatype and value in every message (no
aliases), so any host can decode a data message on its own.

## Behaviour

**Connect.** Clean session. The will is the NDEATH message (QoS 1, not
retained) carrying `bdSeq`. `bdSeq` runs 0 to 255, goes up by one on every
connect including automatic reconnects, and is kept in `data/` so it does not
restart at 0 with the server. After connecting the node subscribes (QoS 1) to
its NCMD topic, its DCMD topics and, when a host ID is set, the host's state
topics.

**Birth.** NBIRTH (sequence number 0) then one DBIRTH per line, each listing
every metric with its current value. The sequence number runs 0 to 255 across
all of the node's messages.

**Data.** Every `sparkplug_interval_ms` the node compares each line's metrics
with what it last sent and publishes one DDATA per line that changed, holding
only the changed metrics. `Last Product` and `Last Code` therefore show the
latest product and code of the interval, not each one.

**Lines change.** A new line: DBIRTH. A deleted line: DDEATH. A renamed line:
DDEATH for the old device ID, DBIRTH for the new one. A metric that did not
exist at birth (a new class is counted, a camera is added): a new DBIRTH for
that line. A stopped line stays a device, with `Line/Running` false.

**Rebirth.** NCMD `Node Control/Rebirth` = true: NBIRTH (sequence number 0,
same `bdSeq`) and every DBIRTH again. Always answered.

**Primary host.** With a host ID set, the node subscribes to
`spBv1.0/STATE/<host id>` (Sparkplug 3.0, JSON `{"online": ..., "timestamp": ...}`)
and `STATE/<host id>` (Sparkplug 2.2, text `ONLINE` / `OFFLINE`). It sends its
births only once the host is online. When the host goes offline (a 3.0 message
older than the last online one is ignored) the node publishes NDEATH,
disconnects, connects again and waits for the host. With no host ID it
publishes as soon as it is connected.

**Disconnect.** When the channel is switched off, Sparkplug is switched off, or
the server shuts down, the node publishes NDEATH itself and then disconnects
(a clean disconnect does not send the will). If the server dies, the broker
sends the will.

**Channel changes.** Saving a channel with different Sparkplug or connection
settings ends the old node (NDEATH) and starts a new one, without a restart.

## Commands

Handled only when `sparkplug_allow_commands` is on; otherwise logged and
ignored (the rebirth request is the exception).

| Message | Effect |
|---|---|
| DCMD `Line/Running` = true / false | starts / stops that line, through the function the dashboard's buttons use |
| DCMD `Commands/Reset Counters` = true | resets that line's counters |

Each one is written to the audit log as user `sparkplug:<channel name>`. Any
other metric, a wrong datatype, or an unknown device is logged and ignored.
After a command the node publishes the metric's real value at once, so a host
never keeps showing a value that was refused.

Anyone who can publish to the broker can send a command. The README says to
give the host its own broker login and to leave the switch off unless needed.

## Bundled broker

`docker-compose.yml` gains a service `mqtt` (image `eclipse-mosquitto:2`) in
the profile `broker`: `docker compose --profile broker up -d` starts it,
plain `docker compose up -d` does not, so existing deployments do not change.

- Login required, no anonymous access. `BROKER_USERNAME` and `BROKER_PASSWORD`
  come from `.env`; a start script writes Mosquitto's password file from them
  and refuses to start with an empty password.
- Listens on 1883, published as `${BROKER_BIND_ADDRESS:-0.0.0.0}:${BROKER_PORT:-1883}`.
- Retained messages and sessions are kept on the named volume `vision-broker`.
- Same hardening as the vision service where it applies: `restart:
  unless-stopped`, `no-new-privileges`, bounded logs, a memory limit, a health
  check.
- Files: `docker/mosquitto/mosquitto.conf`, `docker/mosquitto/start.sh`.

The vision server reaches it as host `mqtt`, port 1883; Ignition and other
devices use the machine's address. The README describes adding the channel.

## Code

| File | Purpose |
|---|---|
| `app/hardware/mqtt/sparkplug_codec.py` (new) | Sparkplug payloads to and from bytes, the datatype codes, topic building and parsing. Uses the `protobuf` package with a message description built in code (no generated file, no `protoc`). No I/O. |
| `app/services/sparkplug_metrics.py` (new) | A line's live state as a list of (name, datatype, value); device ID from a line name. No I/O. |
| `app/services/sparkplug_service.py` (new) | `SparkplugNode`: one channel's connection, `bdSeq`, sequence numbers, births, deaths, data, host state, commands. `SparkplugService`: starts and ends nodes as channels change, runs the publish loop, records the latest product and code of each line. |
| `app/hardware/mqtt/client.py` | Binary will payload; a hook called before every connect (to set the will with the new `bdSeq`); handlers that receive a message's raw bytes. |
| `app/services/settings_persistence_service.py` | The six channel fields and their validation; tells `SparkplugService` when a channel is saved or deleted; the channel test also reports Sparkplug's state. |
| `app/routes/v1/lines.py`, `counting.py` | Start/stop and reset counters become functions that take who did it, shared by the routes and by commands. |
| `app/services/counting_service.py` | Hands each product and code event to `SparkplugService` (one cheap call that never raises). |
| `app/services/health_service.py` | Each MQTT channel in the health report gains `sparkplug`: connected, host online, lines published, last birth, last error. |
| `main.py` | Starts the service after the lines are loaded; stops it (NDEATH) on shutdown, before the MQTT connections close. |
| `dashboard.html` | The channel dialog's "Sparkplug B" section; a badge and state on the channel's card. |
| `requirements.txt` | `protobuf` named explicitly (it is installed today only because ONNX Runtime needs it). |
| `docker-compose.yml`, `.env.example`, `README.md`, `FILE_TREE.md` | The broker service, its settings, and how to connect Ignition. |

## Errors

- Broker unreachable or login refused: the node keeps retrying in the
  background (1 s doubling to 60 s), health shows the broker's reason, nothing
  else on the server is affected.
- A message that cannot be decoded: logged at debug level, ignored.
- A line whose values cannot be read in an interval (for example a camera
  that was removed at that moment): the line keeps its last values for that
  interval; it is not reported as deleted. The other lines are sent.
- The publish loop catches and logs every error per node, so one channel's
  fault does not stop the others.

## Testing

- **Codec:** round trips for every scalar datatype including negative
  integers; decoding of unknown fields; topic parsing.
- **Node, against the in-test broker** (extended with subscribe and message
  delivery): the will carries `bdSeq`; NBIRTH is sequence 0 and followed by
  DBIRTHs; sequence numbers wrap at 255; only changed metrics are sent; a new
  class causes a DBIRTH; rename, add and delete of a line; rebirth; primary
  host online, offline and stale-timestamp messages, in both formats; `bdSeq`
  goes up on reconnect and survives a restart; commands with the switch on and
  off; NDEATH on switch-off.
- **API:** the channel fields are validated and saved; a duplicate node is
  refused; health reports the node.
- **Live:** the scratch server against the Mosquitto on the development
  machine, with messages decoded by Eclipse Tahu's own Python code (installed
  in a throwaway environment outside the project) to prove another
  implementation reads them.
- **Not possible on the development machine:** Ignition itself, and the
  compose service while Docker Desktop is not running. The broker's config
  file is run with the local Mosquitto; the compose service is checked with
  `docker compose --profile broker config` when Docker is available, and by
  the owner or CI otherwise.
