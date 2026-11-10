# Partner guide: register, enroll, report, offer flex

This guide takes you from a fresh checkout to reporting meter data and offering a site's flexibility. Every step is one command, run from the root of this
repository. For background on the API see the [README](../README.md).

The steps, in order:

1. [Set up](#1-set-up)
2. [Register your resources](#2-register-your-resources)
3. [List the programs open to you](#3-list-the-programs-open-to-you)
4. [Enroll resources in a program](#4-enroll-resources-in-a-program)
5. [Report meter data](#5-report-meter-data)
6. [Group resources, push available flex, receive dispatch](#6-group-resources-push-available-flex-receive-dispatch)
7. [Troubleshooting](#7-troubleshooting)

Every command is `python -m flexclient <command>`. `python -m flexclient --help` lists them all, and
`python -m flexclient <command> --help` shows the options of one.

---

## 1. Set up

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` with what Springboard issued you:

| Variable | Meaning |
| --- | --- |
| `FLEXGATEWAY_BASE_URL` | The Flexgateway address, already set in `.env.example` |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | Your OAuth client credentials |
| `FLEXGATEWAY_VEN_NAME` | The name of your VEN, the account your resources live under |
| `FLEXGATEWAY_ACCESS_TOKEN` | Optional. A ready token, used instead of the client credentials |

The client reads `.env` from the directory you run it in. Variables you have already exported win over `.env`.

Check that it works:

```sh
python -m flexclient discover        # prints the token URL; needs no login
python -m flexclient ensure-ven      # logs in, creates your VEN if it does not exist, prints it
```

If `ensure-ven` prints your VEN, your credentials work. Credentials, tokens and secrets are never printed.

---

## 2. Register your resources

A **resource** is one independently controllable unit: a battery, a solar plant, an EV charging site. You describe each
one in a JSON file. `examples/transport-sites.json` has five. Copy it and change the values.

A single resource looks like this:

```json
{
  "objectType": "VEN_RESOURCE_REQUEST",
  "resourceName": "my-company-bergen-battery-001",
  "attributes": [
    {"type": "METER_POINT_ID", "values": ["707057500000000001"]},
    {"type": "RESOURCE_TYPE", "values": ["battery"]},
    {"type": "LOCATION", "values": [5.3221, 60.3913]},
    {"type": "MAX_POWER_CONSUMPTION", "values": [500.0]},
    {"type": "MAX_POWER_EXPORT", "values": [500.0]},
    {"type": "DESCRIPTION", "values": ["Bergen site, battery"]}
  ]
}
```

| Attribute | Meaning |
| --- | --- |
| `resourceName` | Your own unique name for the resource. It is the key you use in every later command. |
| `METER_POINT_ID` | The meter point (GSRN) the resource sits on. Springboard uses it to look up the balance responsible party. |
| `RESOURCE_TYPE` | `battery`, `solar`, `ev_charger` and so on. |
| `LOCATION` | `[longitude, latitude]`, longitude first. |
| `MAX_POWER_CONSUMPTION`, `MAX_POWER_EXPORT` | Limits in kW. |
| `DESCRIPTION` | Free text, up to 128 characters. |

The file may hold one resource or an array of them. Register it:

```sh
python -m flexclient register-resources --resources my-resources.json
```

Registering is **idempotent**: running it again with the same names updates the resources, it never creates copies.

Check what is registered:

```sh
python -m flexclient list-resources
python -m flexclient get-resource <resource id>      # the "id" shown by list-resources
```

A new resource is not yet part of any program. Springboard has to **approve** it before you can enroll it. Tell Springboard
when you have registered resources.

You can register and check the whole flow in one go with `python -m flexclient demo --resources my-resources.json`. It
registers every resource twice to prove that registering is idempotent, then lists your programs and events.

---

## 3. List the programs open to you

```sh
python -m flexclient list-programs
```

You see the programs Springboard has **opened for enrollment**, plus the ones your resources are already in. You never see
anyone else's programs. If a program you expect is missing, it is not open to you yet; ask Springboard.

To find one program by name, use `find-program`. Every word you type must occur in the program name, in any case:

```sh
python -m flexclient find-program "Longflex Arm"      # finds NODES_LONGFLEX_ARMED
```

If nothing matches, or more than one program does, it tells you which programs you can see.

---

## 4. Enroll resources in a program

Once Springboard has approved a resource and opened a program, **you** decide which approved resources take part. Use your
normal credentials; there is no extra login.

Pick the resources in one of three ways:

| Option | Selects |
| --- | --- |
| `--resource NAME` | One resource by `resourceName`. Repeat the option for several. |
| `--name-prefix PREFIX` | Every resource whose name starts with the prefix, such as `sim-` |
| `--all` | Every resource of your VEN |

Always preview first:

```sh
python -m flexclient enroll --program "Longflex Arm" --name-prefix sim- --dry-run
```

Then enroll:

```sh
python -m flexclient enroll --program "Longflex Arm" --name-prefix sim-
python -m flexclient enroll --program "Longflex Arm" --resource my-company-bergen-battery-001
python -m flexclient enroll --program "Longflex Arm" --all
```

`--program` takes the name or some words of it, like `find-program` does. The command prints one line per resource:

| Output | Meaning |
| --- | --- |
| `enrolled` | Done |
| `already enrolled` | It was already in. Re-running is safe. |
| `would enroll` | Shown with `--dry-run`. Nothing changed. |

To withdraw resources again, use the same selection options:

```sh
python -m flexclient unenroll --program "Longflex Arm" --name-prefix sim- --dry-run
python -m flexclient unenroll --program "Longflex Arm" --name-prefix sim-
```

It prints `withdrawn` or `not enrolled` (or `would withdraw` with `--dry-run`).

### If enrolling fails

The client shows the server's reason:

| Status | Meaning |
| --- | --- |
| `403` | Springboard has not approved the resource yet |
| `404` | The program is not open to you, or the resource is not yours |
| `409` | The enrollment window is closed, or Springboard put the resource in a portfolio (it may be part of a bid). Springboard has to release it. |

### See what happens in a program

```sh
python -m flexclient list-events                  # every event you can see
python -m flexclient list-events --active         # only events running now
python -m flexclient list-events --program-id <program id>
```

### Listen for signals

To see events and programs as they change, run:

```sh
python -m flexclient listen --callback-url https://ven.example.com/openadr/callback --port 9301
python -m flexclient listen                      # no public address: polling only
```

It prints one line per signal, for example `[webhook] EVENT CREATE dispatch program=...` followed by the intervals with their
payloads (such as `DISPATCH_SETPOINT_RELATIVE=-50.0`). It first records what you can see now and shows only what changes
afterwards. Webhooks are a hint, so it also polls every 60 seconds (`--poll-seconds`) and shows anything a webhook missed,
marked `[poll]`. Ctrl-C stops it and deletes its subscription.

The callback must be `https` on a public address that forwards to `--host`/`--port` (a tunnel or reverse proxy works).
Details and the rules for failing callbacks are in the "Webhooks" section of the [README](../README.md).

---

## 5. Report meter data

Meter data goes in as readings against **your own `metering` event**, which is created together with your VEN. You do **not**
need to be enrolled in a program. Report from the day you register a resource: what you send once serves every program the
resource joins later, so there is nothing to backfill.

Readings are `DEMAND` in **kW**. **Positive means consumption and negative means generation**, so solar production and
battery discharge are negative.

### Option A: a script you adapt (recommended for a real integration)

[`examples/upload_readings.py`](../examples/upload_readings.py) lists your resources and sends their readings in one step.

```sh
python examples/upload_readings.py --days 90 --dry-run          # show what would be sent
python examples/upload_readings.py --days 90                    # send 90 days of 15-minute readings
python examples/upload_readings.py --days 90 --name-prefix sim- # only resources named sim-*
```

The example invents its data. To send your own, replace the function `read_my_meter_data` in the script with a query against
your meter database. It returns a list of `(interval start, kW)` for one resource. The rest of the script stays as it is.

In your own code the call is:

```python
client.report_readings("my-company-bergen-battery-001", points, payload_type="DEMAND", interval_minutes=15)
```

where `points` is a list of `(datetime with timezone, kW)`.

### Option B: two commands, with files in between

```sh
python -m flexclient generate-readings --out-dir readings --days 30 --interval-minutes 15 --name-prefix sim-
python -m flexclient report-readings --dir readings
```

`generate-readings` writes one JSON file per registered resource with **synthetic** readings. Always give `--name-prefix`
so that you never invent data for a real resource. `report-readings` sends every `*.json` file in the directory. You can
write those files yourself from your real data. Each file holds `resourceName`, `payloadType`, `intervalMinutes` and a list
of `dataPoints` (`interval_start` and `value`).

### Re-running is safe

Readings are sent as one report per resource and UTC week, named so that sending the same data again **replaces** the report.
You can repeat an upload, or send corrected values, without creating duplicates.

### Option C: a report you write yourself

To report what a resource did during a dispatch event, `USAGE` in kWh against its `BASELINE`, post a `reportRequest`:

```sh
python -m flexclient send-report --report examples/report-activation.json
python -m flexclient list-reports
```

Put the id of an event you can see in `eventID` first. This is not the way to send meter history; use option A or B.

---

## 6. Group resources, push available flex, receive dispatch

A **group** is your reporting and dispatch unit, usually one site. You push the group's **available flex** (a forecast per 15 minutes,
36 hours ahead, every 15 minutes) and when Springboard dispatches, **one event arrives for the group**, which you divide across its
resources. The full walkthrough, the rules and the answers you can get are in
**[Resource groups, available flex and dispatch](groups-and-dispatch.md)**. In short:

```sh
python -m flexclient put-group --file examples/site-groups.json              # one group per site; a resource is in one group
python -m flexclient list-groups                                             # members, event target, programs it can be dispatched in
python -m flexclient push-availability --group fjellkraft-bergen --synthetic  # or --file; examples/push_availability.py loops it
python -m flexclient group-events --group fjellkraft-bergen                  # the dispatches addressed to the group
python examples/handle_group_dispatch.py --group fjellkraft-bergen --watch   # receive, split over the resources, report
```

A group can be dispatched in a program only when **every member is enrolled there and approved** (step 4): `list-groups` says which
members are missing.

---

## 7. Troubleshooting

| Problem | What to do |
| --- | --- |
| `provide FLEXGATEWAY_ACCESS_TOKEN or OAuth settings` | Fill in `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` in `.env`, and run from the repository root. |
| `invalid_scope` | Leave `OAUTH_SCOPE` empty. Your client gets all of its scopes by default. |
| `no resources ... registered for this VEN` | Register first (step 2). Check that `FLEXGATEWAY_VEN_NAME` is your VEN. |
| `no program matches ...` | The program is not open to you. Run `list-programs` and ask Springboard. |
| `403` when enrolling | Springboard has not approved the resource. |
| `put-group`: `409` / `422` | A resource is already in another group (the error names it), or the members are on different meter points. See [groups](groups-and-dispatch.md#if-something-is-refused). |
| `push-availability`: `422` | A start is not on a UTC quarter hour, or a value is negative or missing; the error lists each. |
| `this ven has no metering event yet` | Ask Springboard. The server creates the event together with your VEN. |

## All commands at a glance

| Step | Command | Purpose |
| --- | --- | --- |
| Set up | `discover` | Print where tokens are issued (no login) |
| | `ensure-ven` | Create your VEN if missing, and print it |
| | `list-vens` | List VENs |
| Resources | `register-resources --resources FILE` | Create or update resources |
| | `list-resources` | List your resources |
| | `get-resource ID` | Show one resource |
| Programs | `list-programs` | Programs open to you |
| | `find-program NAME` | Find one program by words of its name |
| | `enroll --program P (--resource N \| --name-prefix X \| --all) [--dry-run]` | Enroll resources |
| | `unenroll --program P (--resource N \| --name-prefix X \| --all) [--dry-run]` | Withdraw resources |
| | `list-events [--program-id ID] [--active]` | List events |
| Meter data | `generate-readings --out-dir DIR [--days N] [--interval-minutes N] [--name-prefix X]` | Write synthetic readings |
| | `report-readings --dir DIR` | Send readings from files |
| | `send-report --report FILE` | Post a `reportRequest` |
| | `list-reports` | List your reports |
| Groups | `put-group (--file FILE \| --name N [--resource N ...]) [--dry-run]` | Create or replace a resource group (idempotent) |
| | `list-groups`, `get-group NAME`, `delete-group NAME` | Your groups, with members, target and program eligibility |
| | `push-availability --group G (--file FILE \| --synthetic) [--hours N] [--dry-run]` | Push the group's forecast of available flex |
| | `get-availability --group G [--from T] [--to T]` | What is stored for the group |
| | `group-events --group G [--active]` | Dispatch events addressed to the group, with the split over its resources |
| Notifications | `listen [--callback-url URL] [--port N] [--poll-seconds N]` | Show signals as they arrive |
| | `subscribe`, `list-subscriptions`, `delete-subscription`, `serve-webhooks` | See the "Webhooks" section of the [README](../README.md) |
| Everything | `demo --resources FILE` | Register twice, then list programs and events |
