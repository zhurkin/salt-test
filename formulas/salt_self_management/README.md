# salt_self_management formula

This internal formula provides the candidate activation guard used by
`salt_minion` and `salt_master` during normal highstate.

It installs:

- `/usr/local/libexec/salt-self-management/salt-safe-apply.py`;
- `/etc/salt-self-management/guard.env`;
- `/etc/systemd/system/salt-self-management-apply@.service`.

Normal states render restart-sensitive files under
`/var/lib/salt-installer/self-management/candidate/`. The helper validates the
complete effective Salt configuration, authenticates a changed minion master,
and activates the candidate from an independent systemd oneshot. Failed
activation restores the previous files and records the failed content hash.

The installer uses `execution_mode: installer`, so it keeps its own synchronous
transaction and does not queue this oneshot. `installer.lock` prevents normal
highstate from racing with installer-generated Pillar changes.


The originating highstate only queues a validated candidate. Final activation
is asynchronous and must be checked through
`salt-self-management-apply@minion.service`,
`salt-self-management-apply@master.service`, or the JSON status files below
`/var/lib/salt-installer/self-management/status/`.

Installer and direct-source failed hashes use separate quarantine namespaces;
they share only the most recent verified operational last-good snapshot.

The helper must always run with `/opt/saltstack/salt/bin/python3`. The queue
state and the systemd activation unit both use that interpreter explicitly; the
helper shebang is pinned to the same onedir Python for manual diagnostics.
