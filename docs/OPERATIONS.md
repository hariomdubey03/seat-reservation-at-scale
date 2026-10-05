# Operator runbook

## Deployment layout

The live service is exposed at `https://seats.algocrafter.in` through Cloudflare and
the VM's existing Caddy container. Application files are under
`/opt/seat-reservation/app`; secrets are in `/opt/seat-reservation/secrets.env` (0600).
No SSH key, database password, JWT signing key or live admin token belongs in Git.

`deploy/compose.vm.yaml` uses the dedicated `seat-reservation` project:

- API: 1.5 CPU limit, 768 MiB memory limit, loopback port 18080.
- PostgreSQL 17: 1 CPU limit, 768 MiB memory limit, no host port.
- Persistent database volume: `seat-reservation_reservation_data`.
- Private database network; API also joins the existing `asm_net` proxy network
  using the unique alias `seat-reservation-api`.
- Ten normal DB connections plus one readiness connection per API process.
- Docker restarts both containers unless explicitly stopped. API startup runs the
  idempotent, transactionally locked schema migration before accepting traffic.
- Container logs rotate; the new project has no Docker-socket mount or host filesystem access.

The existing application's containers, database, secrets and network configuration
are not replaced. Caddy receives one additional hostname block from
`deploy/Caddyfile.fragment`. Its configuration is validated, then gracefully reloaded.
The running proxy had an older bind-mounted file inode, so the first deployment
loads the validated candidate at `/config/seat-reservation.Caddyfile`; the host file is
also updated for future container recreation. Do not reload the stale mounted copy
until it has been remounted. The pre-change file and existing container IDs/start times are saved in
`/opt/seat-reservation/backups/`.

## Commands on the VM

```sh
cd /opt/seat-reservation/app
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml ps
curl --fail http://127.0.0.1:18080/health/ready
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml logs --follow reservation-api
```

Only restart this project's API when needed:

```sh
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml restart reservation-api
```

After copying a reviewed release into the application directory:

```sh
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml up --build -d --wait
```

Use the local `compose.yaml` for a clean-checkout development environment. The VM
Compose file intentionally requires an existing proxy network and explicit secrets.
When moving to another VM, set `EDGE_NETWORK` and configure its HTTPS reverse proxy.

## DNS and HTTPS

Cloudflare has a proxied A record for `seats.algocrafter.in` pointing to this VM.
Caddy issues and renews the subdomain's public certificate. The API route sends
`Cache-Control: no-store`; do not introduce a cache rule for state, metrics or logs.
Retain the existing zone security settings. Do not turn off TLS verification to mask
certificate failures. API and database ports remain unpublished or loopback-bound.

The Caddy fragment must survive future deployments of the original application.
If its deployment script regenerates the entire Caddyfile, re-include the fragment
before validation/reload. Keep the unique network alias when recreating this API.

## Backup and restore

Create a database backup before upgrading schema or containers:

```sh
umask 077
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml \
  exec -T db pg_dump -U reservation -d reservation -Fc \
  > /opt/seat-reservation/backups/reservation.dump
```

Copy backups to separate storage and apply a retention policy. Restore into a separate
database/container first, audit it, then arrange a controlled cutover. Never restore
over an active database while accepting reservations. This deployment has persistent
storage; automated off-VM backups and failover are not implemented.

## Rollback and removal

An application rollback requires a previously saved image and compatible schema.
Rebuild the prior source revision only after checking migration compatibility.
Do not use `docker system prune`, restart Docker globally, or run commands against
the existing application's Compose project.

To stop only the new service while preserving its database:

```sh
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml stop
```

Removing the subdomain route requires editing only its Caddy block, validating, and
reloading. Restore the saved full Caddyfile only if nobody has made later changes.
Do not use `down --volumes` unless intentionally deleting all reservation data.

## Incident checks

1. Compare `/health/live` with `/health/ready`; the latter queries the real database.
2. Inspect resource limits, database connection/lock waits and correlated JSON logs.
3. Check `/metrics` and the affected show's reconciliation invariant.
4. Distinguish expected `409` conflicts from `5xx`, proxy errors and client timeouts.
5. Check the existing application too, since the VM and proxy are shared.

For log lookup use the admin-only `/logs` endpoint described in the reviewer guide.
For a full database invariant audit:

```sh
docker compose --env-file /opt/seat-reservation/secrets.env -f deploy/compose.vm.yaml \
  exec -T db psql -v ON_ERROR_STOP=1 -U reservation -d reservation < scripts/reconcile.sql
```

Heavy load should be staged and monitored on this shared VM. Proxy limits and client
connection behavior can affect the public result even if the database remains correct.
Report the measured public outcome, including any failures, separately from local results.
