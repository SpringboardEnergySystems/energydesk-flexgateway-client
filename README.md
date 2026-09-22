# EnergyDesk Flexgateway demo client

This repository is an executable partner-integration example for the EnergyDesk
Flexgateway OpenADR 3 API. It also acts as a small external contract test: if the
demo stops working, either the client or the published API contract has changed.

The first supported journey is intentionally small:

1. obtain an OAuth 2 access token;
2. verify which pre-provisioned VEN the credential belongs to;
3. register one or more independently controllable resources;
4. repeat every resource registration to verify idempotency;
5. list the resources visible to that partner identity.

The client never logs or prints access tokens or client secrets.

## Repository layout

Run installation, tests, and demo commands from the repository root. There is no
need to move into the Python package directory.

```text
energydesk-flexgateway-client/
├── energydesk_flexgateway_client/  # importable Python package
├── examples/                       # sample API payloads
├── tests/
├── .env                            # local secrets; ignored by Git
├── requirements.txt
├── requirements-dev.txt
└── setup.py
```

Keep `.env` at this top level, beside `setup.py` and `requirements.txt`.

## Install

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
cp .env.example .env
```

The CLI automatically loads the root `.env` when run from this directory. Normal
environment variables and command-line options take precedence.

## Run against a local Flexgateway

Migrate both service databases before starting Flexgateway:

```sh
cd ../energydesk-flexgateway-service
sh scripts/migrate_openadr.sh upgrade head
sh scripts/migrate_gateway.sh upgrade head
APP_PORT=8000 python -m main
```

`OPENADR_DEV_AUTH_BYPASS` is deliberately opt-in and must never be enabled in a
shared or deployed environment.

In another shell:

```sh
export FLEXGATEWAY_BASE_URL=http://localhost:8000/openadr3
flexgateway-demo demo --resources examples/transport-sites.json
```

Before running the demo, use the Flexgateway operator portal to create the VEN,
assign a VenAdmin user, and create an API client for that VEN. Put the resulting
client credentials and the appserver token URL in the client repository's `.env`.

## Run with OAuth client credentials

Do not store real credentials in Git. Set them in the environment:

```sh
export FLEXGATEWAY_BASE_URL=https://springboard-test.energydesk.no/flexibility/openadr3
export OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL=https://your-oauth-host.example/o/token/
export OAUTH_CLIENT_ID=provided-by-springboard
export OAUTH_CLIENT_SECRET=provided-out-of-band
export OAUTH_SCOPE='read write'

flexgateway-demo demo --resources examples/transport-sites.json
```

The token request uses the OAuth 2 `client_credentials` grant and HTTP Basic client
authentication. A ready bearer token can instead be supplied through
`FLEXGATEWAY_ACCESS_TOKEN` or `--access-token`.

### Troubleshooting `401 Invalid authentication credentials`

If the token endpoint succeeds but Flexgateway returns `401`, the client has
already acquired and sent a bearer token. Check the Flexgateway **service**
configuration, not the client configuration:

```dotenv
OAUTH_SERVER_URL=http://127.0.0.1:8001
OAUTH_INTROSPECT_ENDPOINT=/o/introspect/
OAUTH_INTROSPECTION_CLIENT_ID=<flexgateway-validator-client-id>
OAUTH_INTROSPECTION_CLIENT_SECRET=<flexgateway-validator-client-secret>
```

The validator credentials must identify a confidential `client_credentials`
application owned by a platform administrator in the same Django instance that
issued the partner token. Flexgateway exchanges these credentials for a short-lived
token carrying the reserved `introspection` scope; VEN-owned partner applications
cannot request that scope. Keep only one definition of each variable in the service
`.env`, and restart both services after changing OAuth settings.

The partner credentials in this repository and the validator credentials in the
service should be separate in a shared environment. A Google/OIDC portal session
is also separate and does not authenticate command-line API requests.

## Individual commands

```sh
flexgateway-demo list-vens
flexgateway-demo register-resource --resource examples/transport-sites.json
flexgateway-demo list-resources
flexgateway-demo get-resource RESOURCE_UUID
```

`register-resource` accepts either one JSON resource object or an array manifest.
For a manifest it registers every entry and prints an array of responses.

All commands accept `--base-url` and `--access-token`. Command-line values override
environment values.

## Resource payload

Do not put a VEN ID or a Flexgateway resource UUID in the request. VEN ownership is
derived exclusively from the bearer token's provisioned OAuth-client assignment,
and Flexgateway creates the resource UUID. An unassigned OAuth client is rejected.

`external_resource_id` is the partner's durable identifier. Repeating a request
with the same bearer identity and `external_resource_id` must return the same
Flexgateway resource ID.

### Resource granularity and types

Register each independently measured or controllable unit as its own resource.
For example, a charging site containing an EV load, a battery and solar panels is
three resources sharing one `service_location.meterpoint_id`; it is not one
`solar+battery` resource. A partner may optimize those units together without
combining their registration identities.

The API uses the EnergyDesk Python SDK's authoritative `ResourceType` taxonomy:

- `battery`, `solar`, and `main_meter` for dedicated-worker device types;
- `ev_charger`, `heat_pump`, `hvac`, `wind`, `generator`, and `load` for generic
  configurable assets;
- `other` as the explicit fallback for a device that cannot yet be classified.

The value also serves as the IoT/InfluxDB measurement name. Flexgateway rejects
arbitrary top-level resource types with HTTP `422`; clients should send `other`
when none of the defined categories applies. More detailed model or technology
information can still be supplied as attributes.

The six-resource [transport-sites.json](examples/transport-sites.json) manifest
models two synthetic transport charging sites. Each site has three distinct
resources sharing its meterpoint: `ev_charger`, `battery`, and `solar`.

## Expected security behavior

- Every API call uses `Authorization: Bearer …`.
- VEN responses never contain a stored bearer token.
- `GET /vens` returns only the caller's VEN.
- Resource list and detail operations return only resources owned by the caller's
  VEN.
- A resource owned by another partner should appear as `404`, avoiding disclosure
  of whether the identifier exists.
- Invalid or expired tokens return `401`.

These behaviors should be covered by service-side authorization tests before the
shared test environment is handed to a partner.

The findings discovered while building this client, including unresolved release
blockers, are tracked in [docs/api-hardening-findings.md](docs/api-hardening-findings.md).

## Tests

The tests use an in-memory HTTP transport and never call a deployed service:

```sh
python -m pytest
```

The `demo` command is the external smoke test for a running environment.
