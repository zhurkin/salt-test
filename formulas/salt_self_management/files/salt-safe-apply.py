#!/opt/saltstack/salt/bin/python3
"""Transactional Salt master/minion configuration activation.

Normal highstates render candidates below the protected state directory.  This
helper validates a complete effective Salt configuration, probes a changed
master target with an isolated PKI copy, and activates the candidate from a
systemd oneshot outside the salt-minion cgroup.  Failed candidates are rolled
back and quarantined by content hash.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import salt.config
import salt.utils.yaml

COMPONENT_FILES = {
    "minion": [
        "/etc/salt/minion.d/10-master.conf",
        "/etc/salt/minion.d/30-schedule.conf",
    ],
    "master": [
        "/etc/salt/master.d/10-network.conf",
        "/etc/salt/master.d/20-auto-accept.conf",
        "/etc/salt/master.d/30-project-roots.conf",
        "/etc/salt/master.d/40-reactor.conf",
    ],
}
SERVICE = {"minion": "salt-minion.service", "master": "salt-master.service"}
GITFS_REQUIRED_FILES = {
    "top.sls",
    "states/salt_self_management.sls",
    "formulas/salt_master/init.sls",
    "formulas/salt_minion/init.sls",
    "formulas/salt_self_management/init.sls",
    "reactor/minion_lifecycle/healthcheck_on_start/handler.sls",
}


class GuardError(RuntimeError):
    pass


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        value = default
    else:
        try:
            value = int(raw, 10)
        except ValueError as exc:
            raise GuardError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise GuardError(f"{name} must be >= {minimum}, got {value}")
    return value


STATE_DIR = env_path("SALT_GUARD_STATE_DIR", "/var/lib/salt-installer/self-management")
CANDIDATE_DIR = env_path("SALT_GUARD_CANDIDATE_DIR", str(STATE_DIR / "candidate"))
INSTALLER_LOCK = env_path("SALT_GUARD_INSTALLER_LOCK", str(STATE_DIR / "installer.lock"))
SALT_INSTALL_DIR = env_path("SALT_GUARD_SALT_INSTALL_DIR", "/opt/saltstack/salt")
HEALTH_TIMEOUT = env_int("SALT_GUARD_HEALTH_TIMEOUT", 20, minimum=1)
PROBE_TIMEOUT = env_int("SALT_GUARD_MINION_PROBE_TIMEOUT", 15, minimum=1)
PROBE_ENABLED = os.environ.get("SALT_GUARD_MINION_PROBE_ENABLED", "yes") == "yes"


def log(message: str) -> None:
    print(message, flush=True)


def stateful(changed: bool, comment: str) -> None:
    safe = comment.replace("'", "")
    print(f"changed={'yes' if changed else 'no'} comment='{safe}'", flush=True)


def run(command: list[str], *, check: bool = True, timeout: int | None = None,
        capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        timeout=timeout,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def ensure_regular_or_absent(path: Path) -> None:
    if path.is_symlink():
        raise GuardError(f"symbolic link is forbidden: {path}")
    if path.exists() and not path.is_file():
        raise GuardError(f"not a regular file: {path}")


def load_json(path: Path) -> dict[str, Any]:
    ensure_regular_or_absent(path)
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise GuardError(f"JSON root must be an object: {path}")
    return value


def manifest_path(component: str) -> Path:
    return CANDIDATE_DIR / component / "manifest.json"


def load_manifest(component: str, path: Path | None = None) -> dict[str, Any]:
    path = path or manifest_path(component)
    data = load_json(path)
    if data.get("component") != component:
        raise GuardError(f"manifest component mismatch: {path}")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise GuardError(f"manifest files must be a non-empty list: {path}")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise GuardError(f"invalid manifest entry: {entry!r}")
        live = entry.get("live")
        desired = entry.get("state")
        candidate = entry.get("candidate")
        if live not in COMPONENT_FILES[component]:
            raise GuardError(f"manifest cannot manage path: {live}")
        if live in seen:
            raise GuardError(f"duplicate manifest path: {live}")
        seen.add(live)
        if desired not in ("present", "absent"):
            raise GuardError(f"invalid desired state for {live}: {desired}")
        if desired == "present":
            if not isinstance(candidate, str) or not candidate:
                raise GuardError(f"candidate path is required for {live}")
            candidate_path = Path(candidate)
            ensure_regular_or_absent(candidate_path)
            if not candidate_path.is_file():
                raise GuardError(f"candidate file is missing: {candidate_path}")
        elif candidate not in (None, ""):
            raise GuardError(f"absent entry must not have candidate path: {live}")
    return data


def desired_hash(manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode()
    digest.update(canonical)
    digest.update(b"\0")
    for entry in sorted(manifest["files"], key=lambda item: item["live"]):
        digest.update(entry["live"].encode())
        digest.update(b"\0")
        digest.update(entry["state"].encode())
        digest.update(b"\0")
        if entry["state"] == "present":
            digest.update(Path(entry["candidate"]).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def service_is_active(component: str) -> bool:
    return run(["systemctl", "is-active", "--quiet", SERVICE[component]],
               check=False).returncode == 0


def service_is_enabled(component: str) -> bool:
    return run(["systemctl", "is-enabled", "--quiet", SERVICE[component]],
               check=False).returncode == 0


def service_matches(manifest: dict[str, Any]) -> bool:
    component = str(manifest["component"])
    desired_running = bool(manifest.get("service_running", True))
    desired_enabled = bool(manifest.get("service_enabled", True))
    return (service_is_active(component) == desired_running and
            service_is_enabled(component) == desired_enabled)


def live_matches(manifest: dict[str, Any]) -> bool:
    for entry in manifest["files"]:
        live = Path(entry["live"])
        ensure_regular_or_absent(live)
        if entry["state"] == "absent":
            if live.exists():
                return False
        else:
            candidate = Path(entry["candidate"])
            if not live.is_file() or live.read_bytes() != candidate.read_bytes():
                return False
    return service_matches(manifest)


def overlay_manifest(root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        target = root / entry["live"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        if entry["state"] == "present":
            shutil.copy2(entry["candidate"], target)
            os.chmod(target, int(str(entry.get("mode", "0644")), 8))


def build_effective_config(manifest: dict[str, Any]) -> Path:
    temp = Path(tempfile.mkdtemp(prefix="salt-guard-validate-", dir="/run"))
    target = temp / "etc" / "salt"
    source = Path("/etc/salt")
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        target.mkdir(parents=True)
    overlay_manifest(temp, manifest)
    component = manifest["component"]
    base = target / component
    if not base.exists():
        base.touch(mode=0o600)
    return temp


def validate_effective(component: str, manifest: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    temp = build_effective_config(manifest)
    config = temp / "etc" / "salt" / component
    try:
        if component == "minion":
            opts = dict(salt.config.minion_config(
                str(config), cache_minion_id=False, ignore_config_errors=False))
            master = opts.get("master")
            if isinstance(master, str):
                if not master.strip():
                    raise GuardError("Salt minion master is empty")
            elif isinstance(master, list):
                if not master or not all(isinstance(x, str) and x.strip() for x in master):
                    raise GuardError("Salt minion master list is invalid")
                if opts.get("master_type") != "failover":
                    raise GuardError("multiple Salt masters require master_type=failover")
                value = opts.get("retry_dns")
                if not isinstance(value, int) or isinstance(value, bool) or value != 0:
                    raise GuardError("Salt minion failover requires retry_dns=0")
                value = opts.get("master_alive_interval")
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise GuardError("Salt minion master_alive_interval must be a positive integer")
                value = opts.get("master_failback_interval")
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise GuardError("Salt minion master_failback_interval must be a nonnegative integer")
                for key in ("master_failback", "random_master"):
                    if not isinstance(opts.get(key), bool):
                        raise GuardError(f"Salt minion {key} must be boolean")
            else:
                raise GuardError("Salt minion master must be string or list")
        else:
            opts = dict(salt.config.master_config(str(config), exit_on_config_errors=True))
            interface = opts.get("interface")
            if not isinstance(interface, str) or not interface.strip():
                raise GuardError("Salt master interface is invalid")
            for key in ("publish_port", "ret_port"):
                value = opts.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
                    raise GuardError(f"Salt master {key} is outside 1..65535")
        if not salt.config._validate_opts(opts):
            raise GuardError(f"Salt {component} option type validation failed")
        return temp, opts
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def master_entries(opts: dict[str, Any]) -> list[str]:
    value = opts.get("master")
    values = [value] if isinstance(value, str) else list(value)
    result: list[str] = []
    for item in values:
        item = str(item).strip()
        if item and item not in result:
            result.append(item)
    return result


def master_file_changed(manifest: dict[str, Any]) -> bool:
    for entry in manifest["files"]:
        if entry["live"] != "/etc/salt/minion.d/10-master.conf":
            continue
        live = Path(entry["live"])
        candidate = Path(entry["candidate"])
        return not live.is_file() or live.read_bytes() != candidate.read_bytes()
    return False


def minion_identity_ready() -> bool:
    return (
        Path("/etc/salt/pki/minion/minion.pem").is_file()
        and Path("/etc/salt/pki/minion/minion.pub").is_file()
    )


def probe_master(master: str, effective_root: Path) -> None:
    if not PROBE_ENABLED or not minion_identity_ready():
        return
    minion_id_file = Path("/etc/salt/minion_id")
    if not minion_id_file.is_file():
        raise GuardError("minion_id is missing for master probe")
    minion_id = minion_id_file.read_text(encoding="utf-8").strip()
    probe = Path(tempfile.mkdtemp(prefix="salt-guard-probe-", dir="/run"))
    try:
        shutil.copytree(effective_root, probe, dirs_exist_ok=True, symlinks=True)
        pki = probe / "pki-probe"
        shutil.copytree("/etc/salt/pki/minion", pki)
        cached_master_key = pki / "minion_master.pub"
        if cached_master_key.exists() or cached_master_key.is_symlink():
            cached_master_key.unlink()
        cache = probe / "cache-probe"
        sock = probe / "run-probe"
        cache.mkdir()
        sock.mkdir()
        override = {
            "id": minion_id,
            "master": master,
            "master_type": "str",
            "verify_env": False,
            "cachedir": str(cache),
            "sock_dir": str(sock),
            "log_file": str(probe / "probe.log"),
            "pki_dir": str(pki),
        }
        minion_d = probe / "minion.d"
        minion_d.mkdir(exist_ok=True)
        (minion_d / "99-salt-guard-probe.conf").write_text(
            json.dumps(override, ensure_ascii=False, indent=2), encoding="utf-8")
        command = [
            str(SALT_INSTALL_DIR / "salt-call"),
            "--config-dir", str(probe),
            "--timeout", str(PROBE_TIMEOUT),
            "--retcode-passthrough",
            "--no-return-event",
            "--log-level=error",
            "--out=json",
            "test.ping",
        ]
        result = run(command, check=False, timeout=PROBE_TIMEOUT + 8)
        if result.returncode != 0:
            raise GuardError(
                f"candidate minion cannot authenticate to master {master}: "
                f"{(result.stdout or '').strip()}")
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def probe_configured_masters(opts: dict[str, Any], effective_root: Path) -> list[str]:
    """Require authentication to at least one configured master.

    Every failover entry is probed. Unavailable backups produce a degraded
    warning, but they do not reject a candidate while another configured master
    accepts the existing minion identity.
    """
    masters = master_entries(opts)
    if not masters or not PROBE_ENABLED:
        return []
    if not minion_identity_ready():
        return []

    available: list[str] = []
    failed: list[str] = []
    failure_details: list[str] = []
    for master in masters:
        try:
            probe_master(master, effective_root)
            available.append(master)
        except Exception as exc:  # pylint: disable=broad-except
            failed.append(master)
            failure_details.append(f"{master}: {exc}")

    if not available:
        raise GuardError(
            "candidate minion cannot authenticate to any configured master: "
            + "; ".join(failure_details)
        )
    if not failed:
        return []

    warning = (
        "failover degraded; authenticated masters="
        + ",".join(available)
        + "; unavailable masters="
        + ",".join(failed)
    )
    log(warning)
    return [warning]


def preflight(component: str, manifest: dict[str, Any]) -> list[str]:
    root, opts = validate_effective(component, manifest)
    try:
        if component == "minion" and master_file_changed(manifest):
            return probe_configured_masters(opts, root / "etc" / "salt")
        return []
    finally:
        shutil.rmtree(root, ignore_errors=True)



def write_status(component: str, state: str, candidate_hash: str = "", reason: str = "") -> None:
    path = STATE_DIR / "status" / f"{component}.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{os.getpid()}"
    payload = {
        "component": component,
        "state": state,
        "candidate_hash": candidate_hash,
        "reason": reason,
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

def failed_file(component: str) -> Path:
    return STATE_DIR / "failed" / "direct" / component


def failed_matches(component: str, candidate_hash: str) -> bool:
    path = failed_file(component)
    if not path.is_file():
        return False
    first = path.read_text(encoding="utf-8").splitlines()
    return bool(first) and first[0] == candidate_hash


def mark_failed(component: str, candidate_hash: str, reason: str) -> None:
    path = failed_file(component)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        f"{candidate_hash}\ntime={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"
        f"\nreason={reason}\n", encoding="utf-8")
    os.chmod(path, 0o600)


def snapshot_files(manifest: dict[str, Any], root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in manifest["files"]:
        live = Path(entry["live"])
        target = root / entry["live"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_regular_or_absent(live)
        if live.is_file():
            shutil.copy2(live, target)
        else:
            marker = Path(str(target) + ".absent")
            marker.touch(mode=0o600)


def restore_files(manifest: dict[str, Any], root: Path) -> None:
    for entry in manifest["files"]:
        live = Path(entry["live"])
        target = root / entry["live"].lstrip("/")
        marker = Path(str(target) + ".absent")
        live.parent.mkdir(parents=True, exist_ok=True)
        if marker.is_file():
            if live.exists() or live.is_symlink():
                live.unlink()
        elif target.is_file():
            atomic_copy(target, live, stat.S_IMODE(target.stat().st_mode))
        else:
            raise GuardError(f"rollback snapshot is missing for {live}")


def atomic_copy(source: Path, target: Path, mode: int = 0o644) -> None:
    ensure_regular_or_absent(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.salt-guard-{os.getpid()}"
    shutil.copyfile(source, temporary)
    os.chmod(temporary, mode)
    os.chown(temporary, 0, 0)
    os.replace(temporary, target)


def apply_files(manifest: dict[str, Any], source_root: Path | None = None) -> None:
    for entry in manifest["files"]:
        live = Path(entry["live"])
        if entry["state"] == "absent":
            ensure_regular_or_absent(live)
            if live.exists():
                live.unlink()
            continue
        source = (source_root / entry["live"].lstrip("/")) if source_root else Path(entry["candidate"])
        atomic_copy(source, live, int(str(entry.get("mode", "0644")), 8))


def component_hash(component: str) -> str:
    payload = bytearray()
    for name in COMPONENT_FILES[component]:
        path = Path(name)
        if path.is_file():
            payload.extend(f"file {name}\n".encode())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            payload.extend(f"{digest}  {name}\n".encode())
        else:
            payload.extend(f"absent {name}\n".encode())
    return hashlib.sha256(payload).hexdigest()


def save_last_good(component: str) -> None:
    root = STATE_DIR / "last-good" / component
    temp = Path(str(root) + f".tmp.{os.getpid()}")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True, mode=0o700)
    manifest = {
        "component": component,
        "files": [{"live": path, "state": "present"} for path in COMPONENT_FILES[component]],
    }
    for path in COMPONENT_FILES[component]:
        live = Path(path)
        target = temp / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if live.is_file():
            shutil.copy2(live, target)
        else:
            Path(str(target) + ".absent").touch(mode=0o600)
    (temp / ".hash").write_text(component_hash(component) + "\n", encoding="utf-8")
    os.chmod(temp / ".hash", 0o600)
    shutil.rmtree(root, ignore_errors=True)
    os.replace(temp, root)


def copy_candidates_to_pending(manifest: dict[str, Any], root: Path) -> dict[str, Any]:
    copied = json.loads(json.dumps(manifest))
    for entry in copied["files"]:
        if entry["state"] != "present":
            continue
        source = Path(entry["candidate"])
        target = root / "candidate" / entry["live"].lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(source, target)
        entry["candidate"] = str(target)
    return copied


ACTIVE_UNIT_STATES = {"active", "activating", "reloading"}
APPLY_WAIT_TIMEOUT = env_int("SALT_GUARD_APPLY_WAIT_TIMEOUT", 210, minimum=1)


def unit_active(component: str) -> bool:
    return service_active_state(
        f"salt-self-management-apply@{component}.service"
    ) in ACTIVE_UNIT_STATES


def wait_apply_idle(component: str, timeout: int) -> None:
    service = f"salt-self-management-apply@{component}.service"
    deadline = time.monotonic() + timeout
    last_state = service_active_state(service)
    while last_state in ACTIVE_UNIT_STATES and time.monotonic() < deadline:
        time.sleep(0.25)
        last_state = service_active_state(service)
    if last_state in ACTIVE_UNIT_STATES:
        raise GuardError(
            f"previous {component} activation did not finish within {timeout}s "
            f"(state={last_state})"
        )


def queue_lock_path(component: str) -> Path:
    path = STATE_DIR / "locks" / f"queue-{component}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@contextlib.contextmanager
def queue_lock(component: str) -> Iterator[None]:
    with queue_lock_path(component).open("w", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def queue(component: str) -> int:
    with queue_lock(component):
        if INSTALLER_LOCK.exists():
            stateful(False, "installer transaction is active; safe apply skipped")
            return 0
        if unit_active(component):
            wait_apply_idle(component, APPLY_WAIT_TIMEOUT)
        if INSTALLER_LOCK.exists():
            stateful(False, "installer transaction became active; safe apply skipped")
            return 0
        pending = STATE_DIR / "pending" / component
        if pending.exists():
            recover(component)
        manifest = load_manifest(component)
        candidate_hash = desired_hash(manifest)
        if failed_matches(component, candidate_hash):
            write_status(component, "quarantined", candidate_hash, "same failed candidate hash")
            raise GuardError(
                f"candidate {component} hash {candidate_hash} is quarantined; fix Pillar/formula or clear marker")
        if live_matches(manifest):
            last_hash = STATE_DIR / "last-good" / component / ".hash"
            current_hash = component_hash(component)
            if not last_hash.is_file() or last_hash.read_text(encoding="utf-8").strip() != current_hash:
                warnings = preflight(component, manifest)
                if bool(manifest.get("service_running", True)):
                    if component == "master":
                        master_health()
                    else:
                        warnings.extend(current_minion_health())
                save_last_good(component)
            marker = failed_file(component)
            if marker.exists():
                marker.unlink()
            write_status(component, "in_sync", candidate_hash,
                         "; ".join(dict.fromkeys(warnings)) if "warnings" in locals() else "")
            stateful(False, f"live {component} configuration already matches candidate")
            return 0

        try:
            warnings = preflight(component, manifest)
        except Exception as exc:
            mark_failed(component, candidate_hash, f"preflight failed: {exc}")
            write_status(component, "rejected", candidate_hash, f"preflight failed: {exc}")
            raise
        if INSTALLER_LOCK.exists():
            stateful(False, "installer transaction became active after preflight; safe apply skipped")
            return 0
        pending.mkdir(parents=True, mode=0o700)
        snapshot_files(manifest, pending / "before")
        copied = copy_candidates_to_pending(manifest, pending)
        (pending / "manifest.json").write_text(
            json.dumps(copied, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        (pending / "hash").write_text(candidate_hash + "\n", encoding="utf-8")
        (pending / "warnings.json").write_text(
            json.dumps(warnings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (pending / "was-active").write_text(
            "yes\n" if run(["systemctl", "is-active", "--quiet", SERVICE[component]],
                            check=False).returncode == 0 else "no\n",
            encoding="utf-8")
        (pending / "was-enabled").write_text(
            "yes\n" if run(["systemctl", "is-enabled", "--quiet", SERVICE[component]],
                            check=False).returncode == 0 else "no\n",
            encoding="utf-8")
        if component == "minion" and master_file_changed(manifest):
            cached = Path("/etc/salt/pki/minion/minion_master.pub")
            cached_target = pending / "before" / cached.as_posix().lstrip("/")
            cached_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if cached.is_file():
                shutil.copy2(cached, cached_target)
            else:
                Path(str(cached_target) + ".absent").touch(mode=0o600)
            (pending / "master-changed").touch(mode=0o600)
        run(["systemctl", "start", "--no-block",
             f"salt-self-management-apply@{component}.service"], capture=True)
        write_status(component, "queued", candidate_hash)
        stateful(True, f"validated {component} candidate queued for protected activation")
        return 0

def global_lock_path() -> Path:
    lock_path = STATE_DIR / "locks" / "global.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return lock_path


@contextlib.contextmanager
def component_lock(component: str) -> Iterator[None]:
    del component
    with global_lock_path().open("w", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def try_component_lock(component: str) -> Iterator[bool]:
    """Try to own recovery without waiting for a live activator."""
    del component
    with global_lock_path().open("w", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def service_active_state(service: str) -> str:
    result = run(
        ["systemctl", "show", service, "--property=ActiveState", "--value"],
        check=False,
    )
    if result.returncode != 0:
        return "unknown"
    return (result.stdout or "").strip()


def wait_active(service: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        last_state = service_active_state(service)
        if last_state == "active":
            return
        if last_state == "failed":
            break
        time.sleep(0.25)
    raise GuardError(
        f"service did not become active within {timeout}s: {service} "
        f"(state={last_state})"
    )


def wait_inactive(service: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        last_state = service_active_state(service)
        if last_state in ("inactive", "failed"):
            return
        time.sleep(0.25)
    raise GuardError(
        f"service did not stop within {timeout}s: {service} "
        f"(state={last_state})"
    )


def request_service_stop(service: str, timeout: int) -> None:
    result = run(["systemctl", "stop", "--no-block", service], check=False)
    if result.returncode != 0:
        raise GuardError(
            f"cannot request stop for {service}: {(result.stdout or '').strip()}"
        )
    wait_inactive(service, timeout)


def request_service_start(service: str, timeout: int) -> None:
    result = run(["systemctl", "start", "--no-block", service], check=False)
    if result.returncode != 0:
        raise GuardError(
            f"cannot request start for {service}: {(result.stdout or '').strip()}"
        )
    wait_active(service, timeout)


def connect_host(interface: str) -> str:
    value = interface.strip()
    if value in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if value == "::":
        return "::1"
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if address.is_unspecified:
        return "::1" if address.version == 6 else "127.0.0.1"
    return value


def wait_tcp(host: str, port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError as exc:
            error = exc
            time.sleep(0.25)
    raise GuardError(f"cannot connect to {host}:{port}: {error}")



def master_uses_gitfs() -> bool:
    path = Path("/etc/salt/master.d/30-project-roots.conf")
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("r", encoding="utf-8") as stream:
        data = salt.utils.yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        return False
    backends = data.get("fileserver_backend")
    if isinstance(backends, str):
        backends = [backends]
    return isinstance(backends, list) and "gitfs" in [str(item) for item in backends]


def parse_salt_json_or_lines(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return [str(item).strip() for item in data if str(item).strip()]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    result: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("- "):
            result.append(line[2:].strip())
    return result


def master_gitfs_health() -> None:
    if not master_uses_gitfs():
        return
    salt_run = str(SALT_INSTALL_DIR / "salt-run")
    timeout = max(60, HEALTH_TIMEOUT + 10)
    update = run([salt_run, "fileserver.update", "backend=gitfs"], check=False, timeout=timeout)
    if update.returncode != 0:
        raise GuardError(f"gitfs fileserver.update failed: {(update.stdout or '').strip()}")
    listing = run(
        [salt_run, "--out=json", "fileserver.file_list", "saltenv=base", "backend=gitfs"],
        check=False,
        timeout=timeout,
    )
    if listing.returncode != 0:
        raise GuardError(f"gitfs fileserver.file_list failed: {(listing.stdout or '').strip()}")
    files = set(parse_salt_json_or_lines(listing.stdout or ""))
    missing = sorted(GITFS_REQUIRED_FILES - files)
    if missing:
        raise GuardError("gitfs fileserver is missing required files: " + ", ".join(missing))


def master_health() -> None:
    opts = dict(salt.config.master_config("/etc/salt/master", exit_on_config_errors=True))
    host = connect_host(str(opts.get("interface", "0.0.0.0")))
    wait_tcp(host, int(opts.get("publish_port", 4505)), HEALTH_TIMEOUT)
    wait_tcp(host, int(opts.get("ret_port", 4506)), HEALTH_TIMEOUT)
    master_gitfs_health()


def current_minion_health() -> list[str]:
    if not PROBE_ENABLED:
        return []
    manifest = {
        "component": "minion",
        "files": [
            {"live": path, "candidate": path, "state": "present", "mode": "0644"}
            for path in COMPONENT_FILES["minion"] if Path(path).is_file()
        ],
    }
    root, opts = validate_effective("minion", manifest)
    try:
        return probe_configured_masters(opts, root / "etc" / "salt")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def clean_persisted_schedule() -> None:
    path = Path("/etc/salt/minion.d/_schedule.conf")
    if not path.exists():
        return
    ensure_regular_or_absent(path)
    with path.open("r", encoding="utf-8") as stream:
        data = salt.utils.yaml.safe_load(stream) or {}
    if not isinstance(data, dict):
        raise GuardError("persisted schedule root must be mapping")
    schedule = data.get("schedule")
    if not isinstance(schedule, dict) or "salt_self_management" not in schedule:
        return
    del schedule["salt_self_management"]
    temporary = path.parent / f".{path.name}.salt-guard-{os.getpid()}"
    with temporary.open("w", encoding="utf-8") as stream:
        salt.utils.yaml.safe_dump(data, stream, default_flow_style=False)
    os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
    os.chown(temporary, 0, 0)
    os.replace(temporary, path)


def restore_cached_master_key(pending: Path) -> None:
    live = Path("/etc/salt/pki/minion/minion_master.pub")
    saved = pending / "before" / live.as_posix().lstrip("/")
    marker = Path(str(saved) + ".absent")
    if marker.is_file():
        if live.exists() or live.is_symlink():
            live.unlink()
    elif saved.is_file():
        atomic_copy(saved, live, stat.S_IMODE(saved.stat().st_mode))


def set_service_enabled(component: str, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    result = run(["systemctl", action, SERVICE[component]], check=False)
    if result.returncode != 0:
        raise GuardError(f"cannot {action} {SERVICE[component]}: {(result.stdout or '').strip()}")


def activate(component: str) -> int:
    pending = STATE_DIR / "pending" / component
    if not pending.is_dir():
        log(f"no pending candidate for {component}")
        return 0
    with component_lock(component):
        if INSTALLER_LOCK.exists():
            shutil.rmtree(pending, ignore_errors=True)
            log("installer transaction became active; pending candidate discarded")
            return 0
        manifest = load_manifest(component, pending / "manifest.json")
        candidate_hash = (pending / "hash").read_text(encoding="utf-8").strip()
        was_active = (pending / "was-active").read_text(encoding="utf-8").strip() == "yes"
        was_enabled = (pending / "was-enabled").read_text(encoding="utf-8").strip() == "yes"
        desired_running = bool(manifest.get("service_running", True))
        desired_enabled = bool(manifest.get("service_enabled", True))
        manage_schedule = bool(manifest.get("manage_schedule", False))
        warnings_path = pending / "warnings.json"
        warnings = json.loads(warnings_path.read_text(encoding="utf-8")) \
            if warnings_path.is_file() else []
        if not isinstance(warnings, list):
            warnings = []
        service = SERVICE[component]
        try:
            request_service_stop(service, HEALTH_TIMEOUT)
            if component == "minion" and (pending / "master-changed").exists():
                cached = Path("/etc/salt/pki/minion/minion_master.pub")
                if cached.exists() or cached.is_symlink():
                    cached.unlink()
            apply_files(manifest)
            if component == "minion" and manage_schedule:
                clean_persisted_schedule()
            set_service_enabled(component, desired_enabled)
            if desired_running:
                request_service_start(service, HEALTH_TIMEOUT)
                if component == "master":
                    master_health()
                else:
                    warnings.extend(current_minion_health())
            save_last_good(component)
            marker = failed_file(component)
            if marker.exists():
                marker.unlink()
            shutil.rmtree(pending, ignore_errors=True)
            write_status(component, "committed", candidate_hash,
                         "; ".join(dict.fromkeys(str(item) for item in warnings if item)))
            log(f"{component} candidate committed and health checked")
            return 0
        except Exception as exc:  # pylint: disable=broad-except
            reason = str(exc)
            log(f"activation failed for {component}: {reason}")
            try:
                request_service_stop(service, HEALTH_TIMEOUT)
            except Exception as stop_error:  # pylint: disable=broad-except
                log(f"cannot fully stop failed {component} candidate: {stop_error}")
            try:
                restore_files(manifest, pending / "before")
                if component == "minion":
                    restore_cached_master_key(pending)
                    if manage_schedule:
                        clean_persisted_schedule()
                set_service_enabled(component, was_enabled)
                if was_active:
                    request_service_start(service, HEALTH_TIMEOUT)
                    if component == "master":
                        master_health()
                    else:
                        current_minion_health()
            except Exception as rollback_error:  # pylint: disable=broad-except
                mark_failed(component, candidate_hash,
                            f"activation failed: {reason}; rollback failed: {rollback_error}")
                write_status(component, "rollback_failed", candidate_hash,
                             f"activation failed: {reason}; rollback failed: {rollback_error}")
                raise GuardError(
                    f"critical rollback failure for {component}: {rollback_error}") from rollback_error
            mark_failed(component, candidate_hash, reason)
            shutil.rmtree(pending, ignore_errors=True)
            write_status(component, "rolled_back", candidate_hash, reason)
            raise GuardError(f"{component} candidate rolled back: {reason}") from exc


def recover(component: str) -> int:
    """Restore an interrupted activation before the daemon starts.

    This command is intended for systemd ExecStartPre, therefore it never calls
    systemctl for the same service and cannot recurse into its own unit.
    """
    pending = STATE_DIR / "pending" / component
    if not pending.is_dir():
        return 0
    owner_unit = f"salt-self-management-apply@{component}.service"
    if service_active_state(owner_unit) in ACTIVE_UNIT_STATES:
        # Type=oneshot remains in ActiveState=activating while ExecStart runs.
        # That is the normal guarded start, not an interrupted transaction.
        return 0
    with try_component_lock(component) as acquired:
        if not acquired:
            # A live queue/activate path owns the transaction.  ExecStartPre must
            # never wait for the same lock while that owner waits for this daemon.
            return 0
        # Close the race between the first state check and lock acquisition.
        if service_active_state(owner_unit) in ACTIVE_UNIT_STATES:
            return 0
        manifest = load_manifest(component, pending / "manifest.json")
        restore_files(manifest, pending / "before")
        if component == "minion":
            restore_cached_master_key(pending)
            if bool(manifest.get("manage_schedule", False)):
                clean_persisted_schedule()
        was_enabled_file = pending / "was-enabled"
        if was_enabled_file.is_file():
            set_service_enabled(
                component, was_enabled_file.read_text(encoding="utf-8").strip() == "yes")
        shutil.rmtree(pending, ignore_errors=True)
        log(f"recovered interrupted {component} activation before service start")
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("queue", "activate", "recover"):
        command = sub.add_parser(name)
        command.add_argument("component", choices=("minion", "master"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.action == "queue":
            return queue(args.component)
        if args.action == "activate":
            return activate(args.component)
        return recover(args.component)
    except subprocess.TimeoutExpired as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pylint: disable=broad-except
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
