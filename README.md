# EnergyDesk Flexgateway reference client (OpenADR 3.1.0)

An executable example of how a partner integrates with the EnergyDesk Flexgateway
VTN, which implements OpenADR 3.1.0. It is also a small external contract test:
if the demo stops working, either this client or the published API has changed.

The client never logs or prints access tokens, client secrets or webhook bearer tokens.

**New here? Read the [partner guide](docs/partner-guide.md): register resources, list programs, enroll and report meter data, step by step.
Offering a site's flexibility? Then [resource groups, available flex and dispatch](docs/groups-and-dispatch.md).
Just want to try it against the test environment? [Test against the Springboard test environment](docs/test-environment.md): ten minutes, synthetic data, ends with a dispatch arriving at your machine.**

## How the integration works

| Step | What you do | Endpoint |
| --- | --- | --- |
| 1 | Find where to get a token | `GET /auth/server` (no authentication) |
| 2 | Get a token with the OAuth client credentials Springboard issued you | `POST /auth/token` |
| 3 | Make sure your `ven` exists (created once, by you or by Springboard) | `GET/POST /vens` |
| 4 | Register your independently controllable resources (idempotent) | `GET/POST/PUT /resources` |
| 5 | Springboard approves each resource and opens programs; you enroll your approved resources | `POST /api/vtn/programs/{id}/resources` (see "Enroll resources in a program") |
| 6 | Read your programs and events | `GET /programs`, `GET /events` |
| 7 | Report what happened, and meter readings | `POST /reports` |
| 8 | Optionally be notified instead of polling | `POST /subscriptions` |
| 9 | Group your resources per site and push the group's available flex | `PUT /api/vtn/groups/{name}`, `PUT /api/vtn/groups/{name}/availability` (see "Resource groups, available flex and dispatch") |
| 10 | Receive a dispatch for a group, split it over the resources and report | an `EVENT` targeted `GROUP:<venName>/<groupName>` |

Things worth knowing, because they differ from older versions of this API:

* **Springboard publishes programs; you enroll.** You see the programs Springboard has opened for enrollment (and the ones your
  resources are in). Springboard approves your resources first; then you enroll them yourself, see "Enroll resources in a program".
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

## Setup

Check out this repository and run everything from its root. There is nothing to install except the dependencies.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt     # requirements-dev.txt adds the test tools
cp .env.example .env                          # then fill in your credentials
```

The CLI loads `.env` from the directory you run it in. Variables already set in the environment win over `.env`,
so a stale `.env` never overrides what you export.

## Run

```sh
python -m flexclient discover
python -m flexclient demo --resources examples/transport-sites.json   # ven + resources, registered twice
python -m flexclient list-programs
python -m flexclient list-events --active
python -m flexclient send-report --report examples/report-activation.json
```

`python -m flexclient --help` lists all commands.

## Enroll resources in a program

Springboard decides which programs exist and opens them for enrollment, and approves your resources. You decide which of
your **approved** resources take part, with your own VEN credentials (no extra login):

```sh
python -m flexclient list-programs                        # the programs open for enrollment, and the ones you are already in
python -m flexclient find-program "Longflex Arm"          # -> NODES_LONGFLEX_ARMED (every word must occur in the name)
python -m flexclient enroll --program "Longflex Arm" --name-prefix sim- --dry-run
python -m flexclient enroll --program "Longflex Arm" --name-prefix sim-     # or --resource NAME (repeatable) or --all
python -m flexclient unenroll --program "Longflex Arm" --name-prefix sim-   # withdraw again
```

`enroll` and `unenroll` work on the resources of `--ven-name` (`FLEXGATEWAY_VEN_NAME`), print one line per resource
(`enrolled`, `already enrolled`, `withdrawn`, or `would enroll` / `would withdraw` with `--dry-run`) and are safe to re-run.
The server decides, and the client shows its reason: `403` the resource is not approved by Springboard yet, `404` the program
is not open to you (or the resource is not yours), `409` already enrolled, the enrollment window is closed, or Springboard has
put the resource in a portfolio (it may be part of a bid: Springboard has to release it). A program that is not listed is
not open yet. `FLEXGATEWAY_VTN_API_URL` overrides the derived `<host>/api/vtn`.

## Resource groups, available flex and dispatch

A **group** is your reporting and dispatch unit, usually one site. You push its **available flex** every 15 minutes, and Springboard's
dispatch arrives as **one event addressed to the group**, which you divide across the group's resources. Full walkthrough:
[docs/groups-and-dispatch.md](docs/groups-and-dispatch.md).

```sh
python -m flexclient put-group --file examples/site-groups.json         # one group per site (idempotent; a resource is in one group)
python -m flexclient list-groups                                        # members, event target, derived site, programs it can be dispatched in
python -m flexclient push-availability --group fjellkraft-bergen --synthetic     # a forecast per 15 minutes, 36 hours ahead
python -m flexclient get-availability --group fjellkraft-bergen
python -m flexclient group-events --group fjellkraft-bergen             # dispatches addressed to the group, with the split by rated power
python -m flexclient delete-group fjellkraft-bergen
```

Three examples show the code a real integration needs (replace the marked functions with your own planning, device control and metering):

| Example | What it shows |
| --- | --- |
| [`examples/push_availability.py`](examples/push_availability.py) | the rolling push: forecast and available flex for the next 36 hours, every 15 minutes (`--once`, `--dry-run`) |
| [`examples/handle_group_dispatch.py`](examples/handle_group_dispatch.py) | receive a dispatch for a group, split the setpoint over its resources, report the delivery (`--watch`, `--report`) |
| [`examples/site-groups.json`](examples/site-groups.json) | the group definitions for the sample sites |

* **A group can be dispatched in a program only when every member is enrolled there and approved.** The server works it out; `list-groups`
  shows `eligible_programs` and which members are missing elsewhere.
* **Pushes are latest-wins per 15-minute interval** and idempotent; a push is all or nothing, and the client checks what the server would refuse
  (a start not on a UTC quarter hour, a negative or missing value) before sending. In code: `client.put_group(...)`,
  `client.push_availability(group, [groups.availability_interval(...)])`, `client.group_events(group)`, `groups.split_setpoint(kw, weights)`.
* A dispatch's setpoint is for the **whole group**, relative to baseline in kW (**negative = up-regulation**), over an absolute period.
  A resource declares the notice it needs with `payloads.resource_request(..., min_lead_time_minutes=30)`.

## Meter data and synthetic readings

Report meter data against **your own `metering` event**; no program is needed (see above). Two commands exercise it with
realistic volume:

```sh
python -m flexclient generate-readings --out-dir readings --days 30 --interval-minutes 15   # one JSON file per registered resource
# only invent data for some of them: --name-prefix sim-   (never for real resources)
python -m flexclient report-readings --dir readings                                          # reports every file
```

To see the whole flow as code, read [`examples/upload_readings.py`](examples/upload_readings.py): about 40 lines that list
your resources and send their readings in one step (`python examples/upload_readings.py --days 90 --dry-run`). Replace
its `read_my_meter_data` function with a query against your own meter data to send real readings.

* The files hold `DEMAND` in kW, **positive for consumption, negative for generation** (solar production and battery
  discharge are negative). The profiles are shaped by the resource's `RESOURCE_TYPE`, and are the same every run for a
  resource.
* `report-readings` sends one report per resource and UTC week to your metering event, named so that **running it again
  replaces the reports instead of adding copies**. In code: `client.report_readings(resource_name, points, payload_type="DEMAND")`.
* The older commands `create-report`, `submit-report-data` and `get-report-data` are gone: they used endpoints that are
  not part of OpenADR 3.1.0. `send-report` posts a `reportRequest` and `list-reports` lists yours.
  [`examples/report-activation.json`](examples/report-activation.json) is a sample `reportRequest` for it: what a resource
  actually did (`USAGE`) against its `BASELINE` in the 15-minute intervals of a dispatch event you can see. Put that event's
  id in `eventID` first. Meter history does not go through it: use `report-readings` or the script above.

## Webhooks

Instead of polling you can subscribe to changes. Your callback must be `https` (plain `http` to `localhost` only
if the server runs with `VTN_WEBHOOK_ALLOW_LOCALHOST`, which is for development).

```sh
export WEBHOOK_BEARER_TOKEN=<a secret of your own choosing>
python -m flexclient serve-webhooks --port 9301                       # development receiver
python -m flexclient subscribe --callback-url http://127.0.0.1:9301 --objects EVENT --operations CREATE,UPDATE
```

To just **watch signals arrive**, use `listen`. It starts the receiver, subscribes, prints one readable line per
notification and deletes the subscription when you press Ctrl-C. It also polls (every 60 s by default), because a webhook
is a hint and the VTN does not queue notifications for a receiver that is down:

```sh
python -m flexclient listen --callback-url https://ven.example.com/openadr/callback --port 9301
python -m flexclient listen            # no public address? polling only
```

* **`WEBHOOK_BEARER_TOKEN` is yours.** You choose it, you send it when subscribing, and the VTN sends it back to
  you in the `Authorization: Bearer` header of every notification. Your receiver must check it
  (`flexclient.webhooks` does, in constant time). It is not related to your OAuth credentials.
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
