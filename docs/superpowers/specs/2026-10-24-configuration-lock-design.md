# Configuration lock

Date: 2026-10-24. Branch `claude/config-lock` (from `main` at `a48f1c8`).

The owner wants a lock mode: while it is on, nobody can change how the system is set
up (lines, cameras, connections, models, product lists), but the line keeps running
and operators keep working. An Admin locks and unlocks it; a Level 2 user can unlock
it only with a lock code the Admin sets.

## Owner's decisions (2026-10-24)

- **Who unlocks:** an Admin with one click; a Level 2 user only by typing the **lock code**
  the Admin set on My account. Level 1 never.
- **What it freezes:** Lines and Line setup, Cameras and Connections, AI models, Product lists.
- **Re-lock:** it stays unlocked until someone locks it again (no timer).
- Presented in the design and not objected to: **Level 2 can lock** too (locking cannot
  harm), and **the Admin is locked as well** (an Admin unlocks before changing anything).

## 1. Who can do what

| | Level 1 | Level 2 | Admin | API key |
|---|---|---|---|---|
| See whether it is locked | yes | yes | yes | yes (`monitor:read`) |
| Lock | no | yes | yes | no |
| Unlock | no | with the lock code | yes, no code | no |
| Set, change or remove the lock code | no | no | yes | no |
| Change locked settings while locked | no | no | no | no |

- Locking an already locked system, or unlocking an unlocked one, is not an error: the
  answer is the current state and nothing is written to the audit log.
- Without a lock code, only an Admin can unlock (a Level 2 user is told so).

## 2. What the lock blocks

The lock blocks every request that **changes saved configuration**. It is checked on the
server, so it holds for the dashboard, API keys, Node-RED and scripts alike.

**Blocked while locked** (changes saved configuration):
- Lines: `POST /lines` (new), `PUT /lines/{id}`, `DELETE /lines/{id}`, `POST /lines/{id}/clone`.
  Line setup's camera cards, count lines, send cards, output actions, inputs and product
  checks are saved through these, plus `POST /send/actions/batch`, `POST /plc/actions`,
  `POST /plc/actions/batch`, `DELETE /plc/actions/{id}`, `POST /plc/actions/reload`,
  `POST /counting/config`.
- Cameras: `POST /cameras`, `PUT`/`PATCH`/`DELETE /cameras/{id}`, `POST /cameras/{id}/connect`,
  `POST /cameras/{id}/disconnect` (the Connect choice is saved), `POST /system/ip-cameras`,
  `DELETE /system/ip-cameras/{id}`.
- Connections and settings: `POST`/`PATCH /system/settings`, `POST /system/endpoints`,
  `PUT`/`DELETE /system/endpoints/{id}`, `POST /mqtt/certs/upload`, `DELETE /mqtt/certs/{name}`,
  `POST /plc/opcua/certs/upload`.
- AI models: `POST /models/register`, `POST /models/upload`, `POST /models/{id}/activate`,
  `PATCH /models/{id}/mode`, `DELETE /models/{id}`.
- Product lists: `POST /products/lists`, `PUT`/`DELETE /products/lists/{id}`,
  `POST /products/lists/{id}/replace`, `POST /products/import`, `POST /products`,
  `PUT`/`DELETE /products/{id}`.
- Rules and flows: `POST /rules`, `PUT`/`DELETE /rules/{id}`, `POST /flows`,
  `PUT`/`DELETE /flows/{id}`, `POST /flows/{id}/deploy`, `POST /actions`,
  `PUT`/`DELETE /actions/{id}`.

**Allowed while locked** (running the line, tests and checks that save nothing):
- Running: `POST /lines/{id}/start`, `POST /lines/{id}/stop`, `PUT /lines/{id}/batch`,
  `POST /lines/{id}/trigger`, `POST /counting/reset`, `POST /alarms/{id}/acknowledge`,
  `POST /control/trigger/{camera_id}`, `POST /actions/{id}/execute`.
- Inspection: `POST /vision/detect`, `POST /vision/detect/camera/{id}`, `POST /qr/decode`,
  `POST /qr/decode/camera/{id}`, `POST /qr/mobile-scan`.
- Tests and checks (send a test or read something; save nothing):
  - test sends: `POST /system/endpoints/{id}/test`, `POST /send/actions/{id}/test`,
    `POST /plc/actions/{id}/test`, `POST /flows/{id}/test`;
  - camera test picture: `POST /cameras/{id}/trigger`;
  - checks: `POST /rules/evaluate`, `POST /lines/{id}/formula/check`, `POST /products/lists/{id}/code/check`;
  - PLC reads: `POST /lines/{id}/plc-inputs/{input_id}/read`, `POST /plc/opcua/scan`;
  - comms tests: `POST /comms/tcp/check`, `POST /comms/tcp/test`, `POST /comms/webhook/test`,
    `POST /comms/endpoints/{id}/test`.
- MQTT session: `POST /mqtt/connect`, `/disconnect`, `/publish`, `/subscribe`.
- The lock itself: `POST /system/lock`, `POST /system/unlock`, `PUT`/`DELETE /system/lock/code`.
- Not touched by the lock at all: every `GET`, sign-in and sign-out, changing one's own
  password, Users & API keys (Admin only, as today), the production records.

The owner's robot bridge (Node-RED, `POST /lines/{id}/trigger` with an API key) keeps working.

**Fail closed.** The server knows the allowed list above, by route. Any other request that
is not a `GET`/`HEAD` is refused while locked, so a route added later is locked until it is
put on the allowed list on purpose. A test lists every route of the API and fails when one
is in neither list (section 7).

**The answer** to a blocked request is `423 Locked`:
`{"detail": "The configuration is locked. An Admin, or a Level 2 user with the lock code, can unlock it."}`.
It comes after the sign-in check (no sign-in: 401 as before) and before the route's own
level check, so a Level 1 user trying a Level 2 change while locked sees 423 instead of 403:
either way nothing changes.

Internal changes are not HTTP requests and are not blocked: a PLC input that starts or stops
the line or sets the batch, the cameras reconnecting, the counters, audit entries.

## 3. Lock code

- Set by an Admin on **My account**: at least 6 characters (as account passwords), at most 128.
  Spaces at both ends are dropped, when the code is set and when it is typed to unlock.
  Stored only as a hash (`hash_password`, as account passwords); never sent back by any route.
- An Admin can change it (the old code stops working at once) or remove it.
- **Wrong codes:** after 5 wrong codes in a row, unlocking with a code is refused for
  5 minutes (`429`, with `Retry-After`), for every Level 2 user. A right code or an Admin's
  unlock clears the count. The count is kept in memory: a restart clears it.

## 4. Saved state

- `system_state.json` gets `config_lock`:
  `{"locked": false, "changed_by": null, "changed_at": null, "code_hash": null, "code_set_by": null, "code_set_at": null}`.
  `changed_by`/`changed_at` are who locked or unlocked last, and when.
- It survives a restart. An existing installation has no `config_lock` and starts
  **unlocked, without a code** (from `DEFAULT_STATE`; no schema upgrade needed).
- `GET /system/settings` and every other view of the state never show `code_hash`
  (`redact_secrets` drops it). `POST /system/settings` cannot change `config_lock`
  (it only takes its known keys, as today).

## 5. API

| Route | Who | Body | Answer |
|---|---|---|---|
| `GET /api/v1/system/lock` | Level 1+, API key `monitor:read` | | status |
| `POST /api/v1/system/lock` | Level 2+ | | status |
| `POST /api/v1/system/unlock` | Admin; Level 2 with the code | `{"code": "..."}` (Level 2) | status; 403 wrong code or no code set; 429 too many tries |
| `PUT /api/v1/system/lock/code` | Admin | `{"code": "..."}` | status; 422 too short or long |
| `DELETE /api/v1/system/lock/code` | Admin | | status |

Status: `{"locked": bool, "changed_by": str|null, "changed_at": iso|null, "code_set": bool, "retry_after": int}`
(`retry_after`: seconds until a code may be tried again, 0 normally).

- The lock routes are for signed-in users only: an API key may read the status
  (`monitor:read`) but not lock, unlock or set the code (the scope table returns `None`).
- `GET /system/poll-changes` carries `config_lock` (the status) in every answer, so every
  open dashboard sees a lock or unlock within its 2 s poll.

**Audit log** (category "Configuration lock"): `CONFIG_LOCKED`, `CONFIG_UNLOCKED` (with
"with the lock code" for a Level 2 unlock), `LOCK_CODE_SET`, `LOCK_CODE_REMOVED`,
`UNLOCK_WRONG_CODE`, `UNLOCK_BLOCKED` (the 5-minute wait started).

## 6. Dashboard

- **Header:** a lock chip beside the server status: a closed padlock "Locked" or an open
  padlock "Unlocked". Clicking it opens a small dialog:
  - unlocked: "Lock the configuration" (Level 2 and Admin);
  - locked: who locked it and when; **Unlock** (Admin), or a lock code field and
    **Unlock** (Level 2); a Level 1 user sees the state only.
- **Locked pages:** Lines, Line setup, Cameras, AI models, Connections, Products, and the
  card dialogs opened from them (send card, output action, input, channel, camera).
  While locked:
  - a banner at the top of the page: "Locked by {user} at {time}: settings can be viewed
    but not changed." with an **Unlock** button for Level 2 and Admin;
  - every field is disabled, except the ones that only choose what to look at
    (line picker, search, view switches);
  - every button that saves, adds, removes or applies is dimmed and does nothing
    but say "The configuration is locked";
  - buttons that run, test or read stay usable: Start/Stop, Reset counts, Read now,
    Test, Take a picture, Refresh.
- **Anything missed** is still refused by the server: a `423` answer to any of the page's
  requests shows the lock message and turns the page to locked at once.
- **My account (Admin only):** a "Configuration lock" panel: whether a code is set, a new
  code with confirmation, **Save code**, **Remove code**.
- The lock does not change the Line dashboard (start, stop, batch, counters, alarms).

## 7. Tests

- Every non-GET route of the protected API is either on the allowed list or on the locked
  list (one test walks all routers; a new route fails it until it is classified), and every
  allowed-list entry is a real route.
- While locked: a sample of locked routes answer 423 for an Admin, a Level 2 user and an API
  key; the allowed routes (start, stop, batch, trigger, reset counts, alarm acknowledge, tests)
  still work; `GET`s work. Unlocked: the same routes work as before.
- Lock, unlock and code: Level 1 cannot lock or unlock; Level 2 can lock, unlocks only with
  the right code, cannot set the code; Admin unlocks without a code; 5 wrong codes give 429
  for 5 minutes (clock injected), a right code clears the count; the code is never in any
  answer (`/system/lock`, `/system/settings`, `/system/poll-changes`); `POST /system/settings`
  with `config_lock` changes nothing.
- The lock survives a reload of `system_state.json`; a file without `config_lock` loads unlocked.
- Audit entries for each action.
- Dashboard markup: the chip, the dialog, the banner, the My account panel, `data-lock` on the
  save buttons of the locked pages; the lock script passes `node --check`.
- A live check on the scratch server with headless Chrome: lock as Admin, a locked page shows
  the banner and disabled fields, a save is refused, start/stop works, unlock as Level 2 with
  the code, the page is editable again.

## Not in this change

- An automatic re-lock timer, a lock per line, locking Users & API keys.
- Asking the Admin's password again before setting the code (an Admin can unlock anyway).
