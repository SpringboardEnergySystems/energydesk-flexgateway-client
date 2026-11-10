# Resource groups, available flex and dispatch

How a site's worth of resources is offered to Springboard and dispatched, step by step. It builds on the
[partner guide](partner-guide.md): you have registered resources (step 2) and Springboard has approved them.

## The idea

| Level | Whose | What it carries |
| --- | --- | --- |
| **Resource** | yours | meter data (per resource), approval, enrollment in programs |
| **Group** | yours | **available flex** (your forecast, every 15 minutes) and the address Springboard's **dispatch** arrives at |
| Portfolio | Springboard's | how Springboard combines flex from several partners; you never see it |

* A group is usually **one site**. You tell Springboard how much flex the group has, and when Springboard asks for it, **one
  event arrives for the whole group**. You decide how the group delivers it: dividing the setpoint across the group's resources
  is your job (the examples show one way).
* Measurements stay per resource: you keep reporting meter data per resource as before.
* **Springboard decides what it does with your flex.** You see a dispatch for your group; you are not told why, or in which
  service it was sold. Your side of the contract is the forecast you push and the delivery you make.

Rules of a group, which the server enforces:

* **A resource is in at most one group**, and **all members sit on the same meter point** (a site). Two sites, two groups.
* A group belongs to your VEN. Group names are yours; nobody else sees them, and another partner may use the same name.
* A group may exist with **no members yet**. It is simply not eligible for anything.
* **A group can be dispatched in a program only when every member is enrolled there and approved.** The server works this out
  every time: you never set it. `list-groups` shows `eligible_programs`, and for each other program which members are missing.
  To make a group eligible, enroll the missing members (`enroll`, step 4 of the guide). Half a group is never dispatched.

The commands below work on your own VEN with your normal credentials. `python -m flexclient <command> --help` shows the options.

## 1. Make a group

A group is described by a name, an optional description and the resources in it. [`examples/site-groups.json`](../examples/site-groups.json)
makes one group per site from the sample resources of [`examples/transport-sites.json`](../examples/transport-sites.json):

```sh
python -m flexclient put-group --file examples/site-groups.json --dry-run      # what would be sent
python -m flexclient put-group --file examples/site-groups.json
python -m flexclient put-group --name my-site --resource battery-1 --resource ev-1 --description "My site"
python -m flexclient list-groups
```

`put-group` **replaces**: the resources you give are the complete membership, so to add one you list all of them. It is safe to
run again (`created` becomes `replaced`, nothing else changes), and a resource that leaves a group simply stops receiving its
dispatches. In code:

```python
group = client.put_group("fjellkraft-bergen", resource_ids, description="Bergen site")
group["target"]            # "GROUP:fjellkraft-ven/fjellkraft-bergen": what events for this group are addressed to
group["location"]          # derived from the members: meter point, coordinates, grid node
group["eligible_programs"], group["ineligible_programs"]
```

Use the `target` the server gives you; never build that string yourself.

| Answer | Meaning |
| --- | --- |
| `409` | A resource is already in another group. The error lists which resources and which group (`error.problem["conflicts"]`, and the CLI prints it). Remove it from the other group first (a `put-group` of that group without it). |
| `422` | A resource is not one of yours, or the members are on different meter points (`error.problem["meterpoint_ids"]`). |
| `404` | (on `get-group`, `delete-group`) no such group of yours. Someone else's group looks exactly like no group. |

Delete a group with `python -m flexclient delete-group NAME`. The resources stay registered; the group's pushed availability goes
with it. It is refused (`409`) while Springboard is using the group: ask Springboard to release it.

## 2. Push available flex

Tell Springboard what the group can offer, as a **forecast per 15 minutes**, from now to **36 hours** ahead. Push it **every 15
minutes** and **straight away when your plan changes**. Each interval carries three numbers, in kW (the average over the
interval, never negative):

| Field | Means |
| --- | --- |
| `USAGE_FORECAST` | what the group will use according to your own plan |
| `DOWN_REGULATION_AVAILABLE` | how much it can **increase** consumption (or reduce generation) if asked |
| `UP_REGULATION_AVAILABLE` | how much it can **decrease** consumption (or increase generation) if asked |

The availability is only meaningful next to the forecast: a battery your plan already discharges at full power has no up-regulation left.

```sh
python -m flexclient push-availability --group fjellkraft-bergen --synthetic --dry-run   # an invented plan, to try things out
python -m flexclient push-availability --group fjellkraft-bergen --synthetic
python -m flexclient push-availability --group fjellkraft-bergen --file my-plan.json     # {"intervals": [{"start": ..., ...}]}
python -m flexclient get-availability --group fjellkraft-bergen                          # what is stored, now + 36 h
python examples/push_availability.py --group fjellkraft-bergen                           # the rolling push, every 15 minutes
```

[`examples/push_availability.py`](../examples/push_availability.py) is the loop a real integration runs: replace its
`read_my_plan` with a call to your own planning, and keep the rest. In code:

```python
from flexclient import groups

rows = [groups.availability_interval(start, forecast_kw=820, down_kw=600, up_kw=10) for start in groups.rolling_window()]
client.push_availability("fjellkraft-bergen", rows)
```

How it behaves, so you can rely on it:

* **The latest push wins, interval by interval.** Every interval you send replaces what is stored for that start time; intervals you
  do not send are left alone. Sending the same data twice changes nothing, so retry freely.
* **A push is all or nothing**, and it is checked first: a `start` not on a UTC quarter hour (`06:00:00Z`, `06:15:00Z` ...), a negative
  or missing value, or an interval other than `PT15M` is a `422` that lists what is wrong (`error.problem["errors"]`). The client
  checks the same things before it sends, so most mistakes never leave your machine.
* A push may hold up to a week of intervals; the rolling 36 hours is 144.
* A group with no members yet accepts a push too.
* Springboard does not dispatch what you have not told it you have: an interval with nothing pushed counts as no availability.

## 3. Declare the notice your resources need

Some resources need time to prepare (a battery warming up, a charger scheduling sessions). Say so on the resource, in minutes:

```python
payloads.resource_request("bergen-battery", resource_type="battery", min_lead_time_minutes=30, ...)
```

Springboard never dispatches a group with less notice than its **slowest** member declares, and a slow resource is not offered in
services that give little notice. Leave it out if the resource needs no preparation. More notice than a resource needs is always
fine: a dispatch may be signalled minutes or hours before it starts.

## 4. Receive a dispatch

When Springboard dispatches your group, **one event** is created, addressed to it:

```json
{
  "eventName": "DISPATCH fjellkraft-bergen 2026-11-10T08:00:00Z",
  "targets": ["GROUP:fjellkraft-ven/fjellkraft-bergen"],
  "intervals": [{"id": 0,
                 "intervalPeriod": {"start": "2026-11-10T08:00:00Z", "duration": "PT2H"},
                 "payloads": [{"type": "DISPATCH_SETPOINT_RELATIVE", "values": [-250.0]}]}]
}
```

* The **period is absolute**: from `start`, for `duration`. It may begin straight away (a direct dispatch) or a while ahead.
* The **setpoint is relative to your baseline, in kW, for the whole group**. **Negative is up-regulation** (less consumption or more
  export), positive is down-regulation (more consumption). A resource you tell nothing keeps to its own plan.
* You get it the usual ways: poll `list-events`, or `listen` (webhooks with a poll as a safety net), which now prints the
  `targets` so you can see a dispatch for a group at a glance.

```sh
python -m flexclient listen                                                 # signals as they arrive (add --callback-url for webhooks)
python -m flexclient group-events --group fjellkraft-bergen                 # the dispatches addressed to the group, and how they split
python examples/handle_group_dispatch.py --group fjellkraft-bergen --watch  # the loop: receive, split, (report)
```

What the group must do is yours to divide. [`examples/handle_group_dispatch.py`](../examples/handle_group_dispatch.py) splits the setpoint
in proportion to each resource's rated power with `groups.split_setpoint` (the parts keep the sign and add up to the setpoint exactly) and calls
an `apply_setpoint` function you replace with real device control. If you know better (a battery at 20 % cannot discharge), use your own rule,
as long as the group as a whole delivers. With `--report` it also reports what each resource did, against the dispatch event:
`USAGE` (kWh) next to `BASELINE` (kW) per 15 minutes, named so that running it again replaces the report instead of adding a copy.
Replace `read_baseline_and_usage` with your real metering.

```python
group = client.get_group("fjellkraft-bergen")
for event in client.group_events(group):                       # events addressed to the group
    for setpoint in payloads.setpoints(event):                 # [{"start", "duration", "kw"}]
        parts = groups.split_setpoint(setpoint["kw"], weights)  # {resource name: kW}
```

## Trying it end to end

On a test environment, with Springboard sending the dispatch:

```sh
python -m flexclient register-resources --resources examples/transport-sites.json
python -m flexclient put-group --file examples/site-groups.json
python -m flexclient enroll --program "Direct Activation" --name-prefix fjellkraft-     # after Springboard approved them
python -m flexclient push-availability --group fjellkraft-bergen --synthetic
python -m flexclient list-groups                                                        # eligible_programs: ["DIRECT_ACTIVATION"]
python examples/handle_group_dispatch.py --group fjellkraft-bergen --watch --report
```

Ask Springboard to send a test dispatch to the group. The terminal prints it, the split over the resources and the reports, within a poll
interval (30 seconds).

## If something is refused

| You see | Why | What to do |
| --- | --- | --- |
| `put-group`: `no resource named ...` | the name is not registered | `register-resources` first (guide, step 2) |
| `put-group`: `409` already in another group | a resource can be in one group only | the error names the group; move it there or remove it |
| `put-group`: `422` meter points | members on different sites | one group per site |
| `push-availability`: `422` | start not on a quarter hour, negative or missing value | the error lists each problem |
| `list-groups`: not eligible for a program | a member is not enrolled and approved there | the entry lists which members; `enroll` them, or ask Springboard to approve |
| `delete-group`: `409` | Springboard is using the group | ask Springboard to release it |
| no dispatch arrives | nothing has been dispatched, or the group is not eligible | `list-groups` (eligibility) and `get-availability` (is your forecast there?) |
