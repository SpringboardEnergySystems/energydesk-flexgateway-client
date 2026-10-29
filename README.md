# EnergyDesk Flexgateway reference client (OpenADR 3.1.0)

An executable example of how a partner integrates with the EnergyDesk Flexgateway
VTN, which implements OpenADR 3.1.0. It is also a small external contract test:
if the demo stops working, either this client or the published API has changed.

The client never logs or prints access tokens, client secrets or webhook bearer tokens.

## How the integration works

| Step | What you do | Endpoint |
| --- | --- | --- |
| 1 | Find where to get a token | `GET /auth/server` (no authentication) |
| 2 | Get a token with the OAuth client credentials Springboard issued you | `POST /auth/token` |
| 3 | Make sure your `ven` exists (created once, by you or by Springboard) | `GET/POST /vens` |
| 4 | Register your independently controllable resources (idempotent) | `GET/POST/PUT /resources` |
| 5 | Springboard approves each resource and enrolls it in a program | done by the operator, not by you (`enroll` below does it with operator credentials) |
| 6 | Read your programs and events | `GET /programs`, `GET /events` |
| 7 | Report what happened, and meter readings | `POST /reports` |
| 8 | Optionally be notified instead of polling | `POST /subscriptions` |

Things worth knowing, because they differ from older versions of this API:

* **Enrollment is done by the operator.** You see a program only after your resource is enrolled in it. A VEN client cannot
  enroll itself; the `enroll` command does it with an operator (BL) client, see "Enroll resources in a program".
* **You see only your own objects.** Visibility follows your OAuth client and the targets granted to you
  (`PROGRAM:<name>`, `VEN:<name>`, `RESOURCE:<id>`). Targets you put on your own objects are ignored.
* **Wire format is camelCase and errors are RFC 7807** (`application/json` bodies with `title`/`detail`).
* **Lists are paged** (`skip`, `limit`); the client follows pages for you.
* **Resource data uses attributes**: `METER_POINT_ID`, `RESOURCE_TYPE`, `LOCATION` (`[longitude, latitude]`),
  `MAX_POWER_CONSUMPTION` / `MAX_POWER_EXPORT` (kW), `DESCRIPTION`. See `examples/transport-sites.json`.
* **Meter data** goes in a report against your **own `metering` event** (`VEN:<venName>`, created with your ven) with
  payload types `USAGE` in kWh and `DEMAND` in kW. It needs no program: report from the day a resource is registered, and
  what you send once serves every program the resource joins later, with nothing to backfill when it is enrolled. A program
  you are enrolled in has its own `metering` event too; you do not need it.
* **Dispatch setpoints are relative to baseline** (`DISPATCH_SETPOINT_RELATIVE`); a negative value is up-regulation.

## Install

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
cp .env.example .env     # then fill in your credentials
```

The CLI loads `.env` from the directory you run it in. Variables already set in the environment win over `.env`,
so a stale `.env` never overrides what you export.

## Run

```sh
flexgateway-demo discover
flexgateway-demo demo --resources examples/transport-sites.json   # ven + resources, registered twice
flexgateway-demo list-programs
flexgateway-demo list-events --active
flexgateway-demo send-report --report examples/report-activation.json
```

`flexgateway-demo --help` lists all commands.

## Enroll resources in a program (operator)

Enrolling is not a VEN operation: a VEN client only sees the programs its resources are already in, and cannot put a
resource into one. An **operator** can, with a **BL client** (kind *BL*, created under *API access* in the portal) and the
`POST /api/vtn/programs/{id}/resources` route that sits beside the OpenADR prefix. Put the operator credentials next to the
VEN's own in `.env`; only `enroll` uses them:

```
OPERATOR_CLIENT_ID=...
OPERATOR_CLIENT_SECRET=...
```

```sh
flexgateway-demo list-programs                              # with the operator client this lists every program
flexgateway-demo find-program "Longflex Arm"                # -> NODES_LONGFLEX_ARMED (every word must occur in the name)
flexgateway-demo enroll --program "Longflex Arm" --name-prefix sim- --dry-run
flexgateway-demo enroll --program "Longflex Arm" --name-prefix sim-     # or --resource NAME (repeatable) or --all
```

`enroll` works on the resources of `--ven-name` (`FLEXGATEWAY_VEN_NAME`), prints one line per resource (`enrolled`,
`already enrolled`, or `would enroll` with `--dry-run`) and is safe to re-run. With the VEN's own credentials it fails with a
clear message: that route needs a BL client. If the program is not found, the error lists the programs those credentials can
see. `FLEXGATEWAY_VTN_API_URL` overrides the derived `<host>/api/vtn`.

## Meter data and synthetic readings

Report meter data against **your own `metering` event**; no program is needed (see above). Two commands exercise it with
realistic volume:

```sh
flexgateway-demo generate-readings --out-dir readings --days 30 --interval-minutes 15   # one JSON file per registered resource
# only invent data for some of them: --name-prefix sim-   (never for real resources)
flexgateway-demo report-readings --dir readings                                          # reports every file
```

* The files hold `DEMAND` in kW, **positive for consumption, negative for generation** (solar production and battery
  discharge are negative). The profiles are shaped by the resource's `RESOURCE_TYPE`, and are the same every run for a
  resource.
* `report-readings` sends one report per resource and UTC week to your metering event, named so that **running it again
  replaces the reports instead of adding copies**. In code: `client.report_readings(resource_name, points, payload_type="DEMAND")`.
* The older commands `create-report`, `submit-report-data` and `get-report-data` are gone: they used endpoints that are
  not part of OpenADR 3.1.0. `send-report` posts a `reportRequest` and `list-reports` lists yours.

## Webhooks

Instead of polling you can subscribe to changes. Your callback must be `https` (plain `http` to `localhost` only
if the server runs with `VTN_WEBHOOK_ALLOW_LOCALHOST`, which is for development).

```sh
export WEBHOOK_BEARER_TOKEN=<a secret of your own choosing>
flexgateway-demo serve-webhooks --port 9301                       # development receiver
flexgateway-demo subscribe --callback-url http://127.0.0.1:9301 --objects EVENT --operations CREATE,UPDATE
```

* **`WEBHOOK_BEARER_TOKEN` is yours.** You choose it, you send it when subscribing, and the VTN sends it back to
  you in the `Authorization: Bearer` header of every notification. Your receiver must check it
  (`energydesk_flexgateway_client.webhooks` does, in constant time). It is not related to your OAuth credentials.
* The VTN stores it encrypted. The key for that (`SUBSCRIPTION_TOKEN_KEY`) belongs to the **server operator**;
  you never need it.
* You are notified only of changes made after you subscribed, and only for objects you are allowed to see.
* Delivery is retried with backoff for about 13 minutes and then given up on, so treat webhooks as a hint and keep a
  periodic poll as the source of truth.
* **If your receiver stops answering, the VTN backs off and comes back by itself.** After a lasting run of failures it
  sends only one notification per 15 minutes; after 48 hours without a success it stops and probes you at 1, 3, 6, 12,
  then every 24 hours; the first `2xx` resumes everything. You never re-enable anything. After 30 days without a success
  the callback is removed and you subscribe again. A short outage costs you nothing.
* **Poll once after an outage** (`list-events`, `list-programs`): notifications skipped while you were failing are not
  sent later.
* A probe is a normal notification of your own subscription (`objectType` `SUBSCRIPTION`); answer `2xx` like any other
  (the development receiver does). Answer **`410 Gone`** to have a callback removed at once.

## Configuration

See `.env.example`. You need `FLEXGATEWAY_BASE_URL`, and either `FLEXGATEWAY_ACCESS_TOKEN` or
`OAUTH_CLIENT_ID` + `OAUTH_CLIENT_SECRET`. The token URL is discovered from `/auth/server`;
`OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL` overrides it.

## Tests

```sh
python -m pytest
```

The tests run the client against an in-process fake VTN whose requests and responses are validated against the
OpenADR 3.1.0 specification (`tests/spec/`, see the README there for provenance and the documented patches).
