# API hardening findings

This is a client-driven review of the partner-facing OpenADR API. “Addressed” means
the change exists in the current local Flexgateway service working tree; it does
not mean it has been committed or deployed.

## Addressed in the first tranche

| Finding | Change | Verification |
|---|---|---|
| Application import created tables without an Alembic revision | Removed import-time `create_all`; Alembic is the sole schema owner | Empty local database migrated to `0003_openadr` |
| OAuth validation accepted every token | Development bypass is now explicit and disabled by default | Service test verifies fail-closed behavior |
| VEN create logs included the Authorization header and token | Token values were removed from those log messages | Local smoke-test logs inspected |
| VEN responses returned the stored bearer token | Removed `auth_token` from the public response model | OpenAPI assertion and live client check |
| VEN list/detail exposed other VENs | Queries are restricted to the authenticated VEN | OpenAPI security assertion; database-level authorization tests still needed |
| Resource list/detail exposed other VENs | Queries are restricted by authenticated `ven_id` | OpenAPI security assertion; database-level authorization tests still needed |
| Partner credentials could create or take over a VEN | Removed public `POST /vens`; VENs are provisioned by platform admins | OpenAPI route assertion |
| Bearer tokens were stored on VEN rows | Added stable OAuth-client → VEN assignments and cleared legacy tokens | Gateway/OpenADR migrations applied locally |
| Resource bodies supplied ownership identity | Removed `id` and `ven_name`; resource ID and VEN are server-assigned | Request-schema assertion |
| Resource type accepted arbitrary partner text | Reused the EnergyDesk SDK `ResourceType` enum as the API input contract | Request-schema validation test |
| `/openadradmin` relied only on an internal-looking URL | Added the platform-admin dependency to the internal application | Focused route/security tests |
| Mutable Pydantic list defaults | Replaced with `default_factory=list` | Service tests pass |

## Blockers before external partner access

### Identity and credential storage

- Flexgateway requires its own valid confidential OAuth application credentials
  to call Django token introspection. Partner token issuance can succeed while
  every API request still fails if these service credentials are stale. Use a
  distinct validator application, fail startup health checks when introspection
  client authentication is rejected, and avoid duplicate dotenv definitions.
- Valid token-to-VEN results are cached for five minutes. Rotation, revocation and
  VEN deactivation clear the process-local cache, but a multi-replica deployment
  still needs shared cache invalidation or a shorter expiry-aware cache policy.
- OAuth scopes are returned by validation but are not enforced by the endpoints.

### Partner versus administrative operations

- Resource status approval/suspension is currently on the external API and is not
  protected by partner ownership or an administrator role.
- Review all program, event, report and telemetry endpoints for the same tenant
  boundary; only the demo journey has been hardened so far.
- The internal Springboard portal currently reads some external OpenADR endpoints.
  Introduce an OIDC-protected internal API path before relying exclusively on
  partner bearer-token authorization, so hardening does not silently break the
  operator portal.

### Contract cleanup

- `ResourceCreate.id` and `ResourceCreate.ven_name` were removed because the
  service generates the database ID and derives ownership from the bearer token.
- VEN provisioning is now a Springboard administrative operation. The public
  client verifies its assigned VEN with `GET /vens`; it cannot call `POST /vens`.
- Return a clear create-versus-update result for idempotent resource registration;
  the current endpoint returns `201` for both.
- Avoid returning raw exception strings in `500` responses and avoid logging full
  validation bodies where partner or device data may be sensitive.
- Meterpoint upsert is global. Confirm that one partner cannot alter location or
  BRP metadata belonging to another partner through a shared meterpoint ID.

## Contract tests to add next

1. Two OAuth principals create separate VENs and resources.
2. Each principal lists only its own VEN and resources.
3. Each principal receives `404` for the other's resource identifier.
4. A principal cannot update the other's resource, status, attributes or
   meterpoint metadata.
5. Expired, revoked, malformed and insufficient-scope tokens fail consistently.
6. Repeating a registration returns the same resource ID without duplicate rows.
7. OpenAPI contains only intended partner operations and marks every protected
   operation with bearer authentication.
8. Operator portal tests continue to pass through its separate OIDC-protected
   interface.
