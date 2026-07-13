# salt_minion formula

The formula owns minion-side operational resources:

- `/etc/salt/minion.d/10-master.conf`;
- `/etc/salt/minion.d/30-schedule.conf` when schedule management is enabled;
- `/etc/logrotate.d/salt-minion` when logrotate management is enabled;
- `salt-minion.service` enabled/running state.

Bootstrap identity and classification remain installer-owned:

- `/etc/salt/minion_id`;
- `/etc/salt/minion.d/20-classification.conf`.

During normal highstate, restart-sensitive files are rendered to a candidate
directory rather than directly to `/etc/salt`. The shared
`salt_self_management` guard validates the complete effective configuration and
probes a changed master list with an isolated copy of the existing minion identity.
A failover list is accepted when at least one master authenticates; unavailable
backups are reported as degraded instead of making the active master unusable.
Only then does an independent systemd oneshot stop the minion, remove the stale
cached master key when necessary, activate the candidate, start the minion, and
verify connectivity. Any failure restores the previous files and cached master
key.

The installer invokes the same templates with
`execution_mode: installer`; in that mode files are rendered directly into the
installer's own synchronous transaction.

The scheduler runs one `state.sls states.salt_self_management` job. On a normal
minion it applies `formulas.salt_minion`; on a local master/minion it also
applies `formulas.salt_master`. `maxrunning: 1` prevents overlap of this
scheduled state job.
With one address the formula renders the normal scalar `master` option. With
multiple addresses it renders `master_type: failover`, periodic liveness checks,
and deterministic failback to the first address. A matching persisted job is accepted; only a stale starter-owned copy is
removed during controlled minion activation. Hidden and foreign jobs are
preserved.

Scheduler and logrotate retain separate `manage` and `enabled` ownership
switches. The logrotate state does not install logrotate or manage its timer.
