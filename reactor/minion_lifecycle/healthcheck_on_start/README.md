# Minion healthcheck on start

This Reactor is a safe starter workflow for the native event:

```text
salt/minion/<minion-id>/start
```

When enabled, the handler asks only the minion that emitted the event to run
`test.version`. It does not change packages, files, services, Pillar, or
states. The mapping is disabled by default.

## Files

- `mapping.yaml` declares the event-to-handler mapping;
- `handler.sls` contains the short Reactor action;
- this README documents purpose, scope, activation, and verification.

The master formula discovers every `/srv/salt/reactor/**/mapping.yaml`,
validates it, and generates one internal file:

```text
/etc/salt/master.d/40-reactor.conf
```

Do not edit `40-reactor.conf` manually.

## Enable

Change only this field in `mapping.yaml`:

```yaml
enabled: true
```

Then apply the protected Salt self-management composition from the master
host:

```bash
MINION_ID="$(cat /etc/salt/minion_id)"
salt "$MINION_ID" state.apply states.salt_self_management
```

Wait for the asynchronous master activation and inspect the authoritative
result:

```bash
while [ "$(systemctl show salt-self-management-apply@master.service \
  -p ActiveState --value)" = "activating" ]; do
  sleep 1
done

cat /var/lib/salt-installer/self-management/status/master.json
cat /etc/salt/master.d/40-reactor.conf
```

A successful change ends in `committed`; an already matching configuration is
reported as `in_sync`.

## Trigger and observe

In a second terminal, wait for the job return addressed to the local minion:

```bash
MINION_ID="$(cat /etc/salt/minion_id)"
salt-run state.event \
  tagmatch="salt/job/*/ret/${MINION_ID}" \
  count=1 \
  pretty=True
```

Then restart the minion after the master configuration is committed:

```bash
systemctl restart salt-minion.service
```

The returned event should contain `fun: test.version`. The master event bus
received the start event and the Reactor targeted only its source minion. The
master journal is also useful for diagnostics:

```bash
journalctl -u salt-master.service --since '-5 minutes' --no-pager
```

## Environment and targeting

This starter handler applies to the exact minion ID carried in the event, so it
works for production, staging, and dev without assigning a Reactor separately
to each minion.

For an environment-specific workflow, keep the event mapping global and use a
compound target inside the handler. For example, a production-only action can
target both the event source and its classification grain:

```jinja
local.some_function:
  - tgt: 'L@{{ minion_id }} and G@deployment_environment:production'
  - tgt_type: compound
```

Reactor is an event trigger, not a replacement for a formula. A real
configuration change should call an existing state/formula. A multi-host,
ordered workflow should call an Orchestrate SLS.

## Disable

Set `enabled: false` and apply `states.salt_self_management` again. The compiler keeps
the mapping source in `/srv/salt` but removes it from the generated active
registry.
