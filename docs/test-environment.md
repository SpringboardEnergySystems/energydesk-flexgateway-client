# Test against the Springboard test environment

A ten-minute path from a fresh checkout to a **dispatch arriving at your machine**, using the sample sites in this repository.
It uses synthetic data only, so you can run it before your own systems are connected. Each step says what you should see.
Background and the other commands: the [partner guide](partner-guide.md) and [resource groups and dispatch](groups-and-dispatch.md).

`.env.example` points at the test environment, which is separate from production.

## What you need from Springboard

Ask for these once. Nothing below works without them.

1. **OAuth client credentials** for your VEN: a client id and a secret.
2. **A VEN name** (or permission to create one with `ensure-ven`).
3. After step 2 below: **approval of your resources**, and the programs opened to you (for the group test: *Direct Activation*).
4. After step 6: **a test dispatch** sent to your group. You cannot send one yourself.

## The steps

Run everything from the root of this repository.

### 0. Install and configure

```sh
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env          # set OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET and FLEXGATEWAY_VEN_NAME
```

### 1. Check that you can log in

```sh
python -m flexclient discover        # prints the token URL; needs no login
python -m flexclient ensure-ven      # prints your VEN; proves your credentials work
```

*Stuck here?* `provide FLEXGATEWAY_ACCESS_TOKEN or OAuth settings` means `.env` is not filled in or you are not in the repository root;
`401` means a wrong id or secret. Nothing is printed that reveals a secret.

### 2. Register the sample resources and group them

```sh
python -m flexclient register-resources --resources examples/transport-sites.json
python -m flexclient put-group --file examples/site-groups.json
python -m flexclient list-groups
```

You should see two groups, `fjellkraft-bergen` and `fjellkraft-oslo`, with three members each. Both commands are safe to repeat.
Tell Springboard that your resources are registered so they can be **approved**.

### 3. Enroll them

Once Springboard has approved the resources and opened the program:

```sh
python -m flexclient list-programs                                     # is "Direct Activation" listed?
python -m flexclient enroll --program "Direct Activation" --name-prefix fjellkraft-bergen- --dry-run
python -m flexclient enroll --program "Direct Activation" --name-prefix fjellkraft-bergen-
python -m flexclient list-groups
```

`list-groups` should now show `eligible_programs` containing `DIRECT_ACTIVATION` for `fjellkraft-bergen`. A group is eligible only when
**every member** is enrolled and approved; if it is not, the entry lists the members that are missing. `403` on enroll means the
resource is not approved yet.

### 4. Push available flex

```sh
python -m flexclient push-availability --group fjellkraft-bergen --synthetic
python -m flexclient get-availability --group fjellkraft-bergen
```

You should see 144 intervals (36 hours, one per 15 minutes). Pushes are idempotent, so repeating is harmless.
For a rolling push every 15 minutes, as a real integration does: `python examples/push_availability.py --group fjellkraft-bergen`.

### 5. Start listening

In a second terminal:

```sh
python examples/handle_group_dispatch.py --group fjellkraft-bergen --watch --report
```

It polls every 30 seconds, so no public address or webhook is needed.

### 6. Ask Springboard for a test dispatch

Send them: *your VEN name, the group name (`fjellkraft-bergen`), the program (`Direct Activation`), and that your forecast has been pushed.*
They choose the size and period of the dispatch.

Within one poll interval the terminal from step 5 prints the event, the **whole-group setpoint** (kW, relative to baseline,
**negative means up-regulation**), the split over the group's three resources and the report it sends back.
Check afterwards with:

```sh
python -m flexclient group-events --group fjellkraft-bergen
python -m flexclient list-reports
```

The split in the example is proportional to rated power. That is only an example: replacing it with real device control is the part of
the integration that is yours (`examples/handle_group_dispatch.py`).

## When it does not work

| You see | Likely cause | What to do |
| --- | --- | --- |
| `401` or `invalid_scope` | wrong credentials; `OAUTH_SCOPE` set | check `.env`; leave `OAUTH_SCOPE` empty |
| `no program matches ...` | the program is not open to you | `list-programs`, then ask Springboard |
| `403` when enrolling | the resource is not approved yet | ask Springboard to approve it |
| `list-groups`: not eligible | a member is not enrolled and approved | the entry names the member |
| `push-availability`: `422` | a start is not on a UTC quarter hour, or a value is negative or missing | the error lists each problem |
| no dispatch arrives | nothing sent yet, group not eligible, or no forecast pushed | `list-groups`, `get-availability`, then ask Springboard |

More in [troubleshooting](partner-guide.md#7-troubleshooting) and [If something is refused](groups-and-dispatch.md#if-something-is-refused).

## Moving on from the sample

Replace the sample names with your own resources and sites in copies of `examples/transport-sites.json` and `examples/site-groups.json`
(one group per site, all its resources on the same meter point), replace `--synthetic` with your own forecast, and run
`examples/push_availability.py` as a service. Meter data has its own flow that needs no program: see
[step 5 of the guide](partner-guide.md#5-report-meter-data).

Before real use, agree with Springboard how long your forecast has to have been running stably. Pushing every 15 minutes for a week
is a sensible rehearsal.
