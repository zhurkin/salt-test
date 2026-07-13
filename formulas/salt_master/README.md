# salt_master formula

The formula owns master-side operational resources:

- `/etc/salt/master.d/10-network.conf`;
- `/etc/salt/master.d/20-auto-accept.conf`;
- `/etc/salt/master.d/30-project-roots.conf`;
- `/etc/salt/master.d/40-reactor.conf`, compiled from the project Reactor
  registry;
- `/etc/logrotate.d/salt-master` when logrotate management is enabled;
- `salt-master.service` enabled/running state.

It is assigned only to a minion carrying the installer-owned role grain
`roles: [salt_master]`.

During normal highstate, restart-sensitive files are rendered as candidates.
The `salt_self_management` guard loads the complete effective master
configuration with Salt's bundled Python before activation. An independent
systemd oneshot then starts/restarts the master, checks systemd state and both
publish/return ports, and commits last-good. Failure restores the previous
files and restarts the previous master configuration.

The installer uses the same templates in `execution_mode: installer` and keeps
its separate synchronous transaction. Master log rotation remains independent
from minion log rotation and does not restart the daemon.

## Reactor registry

The formula installs `/usr/local/libexec/salt-master/reactor-registry.py`. It
recursively validates `/srv/salt/reactor/**/mapping.yaml`, verifies that every
handler is a regular `.sls` file below the Reactor root, and deterministically
compiles enabled mappings into the single protected `40-reactor.conf`. Users
manage functional mapping directories in `/srv/salt/reactor`; the generated
master config is not edited directly.

## Fileserver modes

The starter supports two master fileserver modes:

- `roots` (default): states, formulas, and Reactor handlers are served from
  the local `/srv/salt` project tree;
- `gitfs`: states, formulas, and Reactor handlers are served from one HTTPS
  GitFS project repository using the `gitcli` provider. The master needs
  system Git 2.3.0 or newer; this provider does not support submodules.

Both modes expose the same namespace rooted at the project root. GitFS connects
the repository once; it does not merge repeated remotes. Pillar remains local
under `/srv/pillar` and is not exposed through `salt://`.

The protected roots-to-GitFS installer transaction compiles Reactor from the
configured local project root while the running master still serves roots. Its
post-restart health gate then updates GitFS and verifies required remote files;
it does not compare the two trees. Later normal highstates compile Reactor
through the active GitFS backend.
