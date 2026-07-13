# Salt monorepo

This starter tree uses one stable Salt saltenv named `base`. Bootstrap identity
and classification are installer-owned:

- `/etc/salt/minion_id`;
- `/etc/salt/minion.d/20-classification.conf`.

The installer can materialize this same template either as the local roots
project or as an independent Git-ready seed below its workspace. Those trees
are not synchronized. The seed is a starting point for a user-managed Git
repository and is generated independently of the local project.

Pillar is stored separately under `/srv/pillar` and layered as base,
environment, role, optional cluster, and optional host override. It is never
published through the fileserver.

Internal formulas:

- `salt_minion` owns minion configuration, scheduler, logrotate, and service
  desired state;
- `salt_master` owns master configuration, the generated Reactor registry,
  logrotate, and service desired state;
- `salt_self_management` provides guarded candidate activation for both.

Normal highstate does not write restart-sensitive Salt configuration directly
to `/etc/salt`. It renders candidates below
`/var/lib/salt-installer/self-management/candidate`, validates them with the
installed Salt config loader, probes a changed minion master list, and queues an
independent systemd activation. A failover list requires at least one reachable,
authenticated master and records unavailable backups as degraded. Successful
candidates become last-good. Failed candidates are rolled back and quarantined
by content hash.

The installer is a separate safety layer. It creates `installer.lock`, updates
its generated Pillar, renders formulas in `execution_mode: installer`, and
commits or restores both the generated Pillar and operational files. Scheduled
and normal highstate skip while that lock exists.

A malformed Jinja/SLS prevents candidate rendering; invalid Salt YAML/options
are rejected before restart; unreachable new masters do not interrupt the old
minion connection; runtime start/health failures restore the previous files.
The guard protects only resources that continue to use these formulas. A team
that replaces them with direct `/etc/salt` writes also assumes the safety
lifecycle.

Scheduler and logrotate support `manage: false` for an explicit ownership
handoff. There is no shared logrotate formula: minion and master rotate only
their own logs.

Layout:

- `top.sls` - fileserver top file shared by roots and GitFS modes;
- `states/` - explicit state compositions;
- `formulas/` - component formulas and the activation guard;
- `reactor/` - functional event mappings, handlers, and README files; the
  master formula compiles enabled mappings into one protected config.

The separate `/srv/pillar` tree contains `top.sls`, `base/`, `environments/`,
`roles/`, `clusters/`, and `hosts/`.

## Fileserver modes

The starter supports two master fileserver modes:

- `roots` (default): states, formulas, and Reactor handlers are served from
  the local `/srv/salt` project tree;
- `gitfs`: states, formulas, and Reactor handlers are served from one HTTPS
  GitFS project repository using the `gitcli` provider. The master needs
  system Git 2.3.0 or newer; this provider does not support submodules.

Both modes expose the same namespace: `states.*`, `formulas.*`, and
`salt://reactor/...`. GitFS connects the project repository once without
mountpoint or subdirectory merging. Pillar remains local under `/srv/pillar`;
`git_pillar` is not configured.
