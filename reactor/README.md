# Reactor registry

This directory is the declarative source of Salt Reactor mappings and handlers.
Users do not edit `/etc/salt/master.d/40-reactor.conf` directly.

Each functional reaction lives in its own directory:

```text
/srv/salt/reactor/<functional-area>/<reaction-name>/
├── mapping.yaml
├── handler.sls
└── README.md
```

The `salt_master` formula recursively discovers `mapping.yaml` files, validates
their schema and handler paths, aggregates enabled mappings by event tag, and
generates one protected master configuration file.

## Mapping schema

```yaml
schema_version: 1
name: unique_lowercase_name
enabled: false
event:
  tag: 'event/tag/*'
handler:
  source: 'reactor/functional-area/reaction-name/handler.sls'
```

Rules:

- `name` is globally unique and matches `[a-z][a-z0-9_]{0,63}`;
- `enabled` is a YAML boolean;
- `event.tag` is the Salt event tag or glob;
- `handler.source` is a relative `.sls` path in the fileserver namespace and
  must begin with `reactor/`;
- symbolic links and paths escaping the Reactor root are rejected;
- multiple enabled mappings may share one tag and are aggregated into one
  generated list;
- an invalid mapping stops candidate generation before the master is restarted.

The active registry is generated at:

```text
/etc/salt/master.d/40-reactor.conf
```

Normal `/srv/salt` changes go through candidate validation, protected master
restart, runtime health checks, rollback, and bad-hash quarantine.

## Responsibilities

- formulas describe desired machine state;
- formula requisites such as `watch` and `onchanges` react inside one state run;
- scheduler starts work by time;
- Reactor starts work from an event;
- Orchestrate coordinates ordered work across multiple minions.

Keep Reactor handlers short. They should usually call an existing formula/state
or start an Orchestrate workflow rather than embed a large deployment process.
