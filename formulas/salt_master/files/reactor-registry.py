#!/opt/saltstack/salt/bin/python3
"""Compile declarative Reactor mappings into one Salt master config file."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import salt.utils.yaml as yaml
except ImportError:  # Allows local static testing outside Salt onedir.
    import yaml  # type: ignore[no-redef]

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAPPING_KEYS = {"schema_version", "name", "enabled", "event", "handler"}
EVENT_KEYS = {"tag"}
HANDLER_KEYS = {"source"}


class RegistryError(RuntimeError):
    """Raised when the Reactor registry is unsafe or invalid."""


def fail(message: str) -> None:
    raise RegistryError(message)


def ensure_plain_string(value: Any, field: str, source: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{source}: {field} must be a non-empty string")
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        fail(f"{source}: {field} contains a control character")
    return value


def ensure_regular_tree_path(root: Path, path: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        fail(f"{label} escapes registry root: {path}")

    current = root
    if root.is_symlink():
        fail(f"registry root cannot be a symbolic link: {root}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            fail(f"{label} cannot contain a symbolic link: {current}")

    if not path.is_file():
        fail(f"{label} is not a regular file: {path}")


def load_mapping(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream)
    except Exception as exc:  # Salt/PyYAML exceptions differ by release.
        fail(f"{path}: cannot parse YAML: {exc}")

    if not isinstance(data, dict):
        fail(f"{path}: mapping root must be a dictionary")

    unknown = set(data) - MAPPING_KEYS
    missing = MAPPING_KEYS - set(data)
    if unknown:
        fail(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        fail(f"{path}: missing keys: {', '.join(sorted(missing))}")

    if data["schema_version"] != 1:
        fail(f"{path}: schema_version must be 1")

    name = ensure_plain_string(data["name"], "name", path)
    if not NAME_RE.fullmatch(name):
        fail(f"{path}: name must match {NAME_RE.pattern}")

    if not isinstance(data["enabled"], bool):
        fail(f"{path}: enabled must be a YAML boolean")

    event = data["event"]
    if not isinstance(event, dict) or set(event) != EVENT_KEYS:
        fail(f"{path}: event must contain exactly: tag")
    tag = ensure_plain_string(event["tag"], "event.tag", path)

    handler = data["handler"]
    if not isinstance(handler, dict) or set(handler) != HANDLER_KEYS:
        fail(f"{path}: handler must contain exactly: source")
    handler_source = ensure_plain_string(handler["source"], "handler.source", path)

    return {
        "name": name,
        "enabled": data["enabled"],
        "tag": tag,
        "handler_source": handler_source,
    }


def validate_handler(root: Path, mapping_file: Path, source: str) -> Path:
    if source.startswith(("/", "salt://")) or "\\" in source:
        fail(f"{mapping_file}: handler.source must be relative to the project root")

    parts = Path(source).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        fail(f"{mapping_file}: unsafe handler.source: {source}")
    if not source.endswith(".sls"):
        fail(f"{mapping_file}: handler.source must end with .sls")

    handler = root.joinpath(*parts)
    ensure_regular_tree_path(root, handler, "Reactor handler")
    return handler


def discover(project_root: Path, registry_prefix: str) -> tuple[dict[str, list[str]], int]:
    registry_root = project_root.joinpath(*safe_fileserver_path(registry_prefix))
    if project_root.is_symlink() or registry_root.is_symlink():
        fail(f"registry root cannot be a symbolic link: {registry_root}")
    if not registry_root.is_dir():
        fail(f"registry root is not a directory: {registry_root}")

    mappings_by_tag: dict[str, list[str]] = defaultdict(list)
    seen_names: dict[str, Path] = {}
    seen_pairs: dict[tuple[str, str], Path] = {}
    discovered = 0

    for mapping_file in sorted(registry_root.rglob("mapping.yaml")):
        ensure_regular_tree_path(registry_root, mapping_file, "Reactor mapping")
        mapping = load_mapping(mapping_file)
        discovered += 1

        expected_prefix = registry_prefix.rstrip("/") + "/"
        if not mapping["handler_source"].startswith(expected_prefix):
            fail(
                f"{mapping_file}: handler.source must begin with "
                f"{expected_prefix!r}"
            )

        name = mapping["name"]
        if name in seen_names:
            fail(
                f"{mapping_file}: duplicate name {name!r}; "
                f"first declared in {seen_names[name]}"
            )
        seen_names[name] = mapping_file

        validate_handler(project_root, mapping_file, mapping["handler_source"])
        if not mapping["enabled"]:
            continue

        pair = (mapping["tag"], mapping["handler_source"])
        if pair in seen_pairs:
            fail(
                f"{mapping_file}: duplicate enabled event/handler pair; "
                f"first declared in {seen_pairs[pair]}"
            )
        seen_pairs[pair] = mapping_file
        mappings_by_tag[mapping["tag"]].append(mapping["handler_source"])

    normalized = {
        tag: sorted(sources)
        for tag, sources in sorted(mappings_by_tag.items())
    }
    return normalized, discovered


def render(source: str, mappings: dict[str, list[str]]) -> str:
    lines = [
        "# Managed by Salt formula salt_master.",
        f"# Generated from {source}/**/mapping.yaml; do not edit.",
    ]

    if not mappings:
        lines.append("reactor: []")
        return "\n".join(lines) + "\n"

    lines.append("reactor:")
    for tag, sources in mappings.items():
        lines.append(f"  - {json.dumps(tag, ensure_ascii=False)}:")
        for source in sources:
            uri = f"salt://{source}"
            lines.append(f"    - {json.dumps(uri, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def ensure_output_target(path: Path) -> None:
    if path.is_symlink():
        fail(f"output cannot be a symbolic link: {path}")
    if path.exists() and not path.is_file():
        fail(f"output exists and is not a regular file: {path}")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)


def write_atomic(path: Path, content: str) -> bool:
    ensure_output_target(path)
    encoded = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        os.chmod(path, 0o644)
        return False

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True



def run_command(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
    )
    if result.returncode != 0:
        fail(f"command failed ({result.returncode}): {' '.join(command)}: {(result.stdout or '').strip()}")
    return result.stdout or ""


def parse_json_output(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        # Salt sometimes emits log lines before the JSON payload.  Try the last
        # non-empty line before falling back to the caller's text parser.
        for line in reversed(stripped.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def normalize_file_list(raw: Any, text: str) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                return [str(item).strip() for item in value if str(item).strip()]
    result: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- "):
            result.append(line[2:].strip())
    return result


def fileserver_list(salt_run: str, saltenv: str, backend: str) -> list[str]:
    command = [
        salt_run,
        "--out=json",
        "fileserver.file_list",
        f"saltenv={saltenv}",
    ]
    if backend:
        command.append(f"backend={backend}")
    text = run_command(command)
    files = normalize_file_list(parse_json_output(text), text)
    if not files:
        fail("fileserver returned an empty file list")
    return sorted(dict.fromkeys(files))


def extract_salt_call_value(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        # salt-call usually returns {"local": "..."}.
        for value in raw.values():
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and isinstance(value.get("return"), str):
                return value["return"]
    fail("salt-call did not return a file string")


def fileserver_read(salt_call: str, saltenv: str, path: str) -> str:
    text = run_command([
        salt_call,
        "--out=json",
        "--retcode-passthrough",
        "cp.get_file_str",
        f"salt://{path}",
        f"saltenv={saltenv}",
    ])
    return extract_salt_call_value(parse_json_output(text))


def safe_fileserver_path(path: str) -> list[str]:
    parts = Path(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        fail(f"unsafe fileserver path: {path}")
    if path.startswith("/") or "\\" in path:
        fail(f"unsafe fileserver path: {path}")
    return list(parts)


def compile_registry_fileserver(
    output: Path,
    *,
    salt_run: str,
    salt_call: str,
    saltenv: str,
    backend: str,
    registry_prefix: str,
) -> None:
    files = fileserver_list(salt_run, saltenv, backend)
    file_set = set(files)
    prefix = "/".join(safe_fileserver_path(registry_prefix)).rstrip("/") + "/"
    mapping_files = [
        item for item in files
        if item.startswith(prefix) and item.endswith("/mapping.yaml")
    ]
    if not mapping_files:
        content = render(f"salt:// ({backend or 'active fileserver'})", {})
        changed = write_atomic(output, content)
        print(json.dumps({"changed": changed, "comment": "reactor registry compiled: discovered=0, enabled=0, tags=0"}, ensure_ascii=False))
        return

    with tempfile.TemporaryDirectory(prefix="reactor-registry-fileserver-") as temp_name:
        root = Path(temp_name)
        for item in files:
            if item.endswith(".sls"):
                target = root.joinpath(*safe_fileserver_path(item))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch(mode=0o644)
        for item in mapping_files:
            target = root.joinpath(*safe_fileserver_path(item))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fileserver_read(salt_call, saltenv, item), encoding="utf-8")

        mappings_by_tag: dict[str, list[str]] = defaultdict(list)
        seen_names: dict[str, Path] = {}
        seen_pairs: dict[tuple[str, str], Path] = {}
        discovered = 0
        for mapping_file in sorted(root.rglob("mapping.yaml")):
            mapping = load_mapping(mapping_file)
            discovered += 1
            if not mapping["handler_source"].startswith(prefix):
                fail(
                    f"{mapping_file}: handler.source must begin with "
                    f"{prefix!r}"
                )
            name = mapping["name"]
            if name in seen_names:
                fail(f"{mapping_file}: duplicate name {name!r}; first declared in {seen_names[name]}")
            seen_names[name] = mapping_file

            validate_handler(root, mapping_file, mapping["handler_source"])
            if mapping["handler_source"] not in file_set:
                fail(f"{mapping_file}: handler.source is not present in fileserver: {mapping['handler_source']}")
            if not mapping["enabled"]:
                continue
            pair = (mapping["tag"], mapping["handler_source"])
            if pair in seen_pairs:
                fail(f"{mapping_file}: duplicate enabled event/handler pair; first declared in {seen_pairs[pair]}")
            seen_pairs[pair] = mapping_file
            mappings_by_tag[mapping["tag"]].append(mapping["handler_source"])

        normalized = {tag: sorted(sources) for tag, sources in sorted(mappings_by_tag.items())}
        content = render(f"salt:// ({backend or 'active fileserver'})", normalized)
        changed = write_atomic(output, content)
        enabled = sum(len(sources) for sources in normalized.values())
        comment = f"reactor registry compiled: discovered={discovered}, enabled={enabled}, tags={len(normalized)}"
        print(json.dumps({"changed": changed, "comment": comment}, ensure_ascii=False))


def compile_registry(root: Path, output: Path, registry_prefix: str) -> None:
    if root.is_symlink():
        fail(f"registry root cannot be a symbolic link: {root}")
    root = root.resolve(strict=True)
    mappings, discovered = discover(root, registry_prefix)
    content = render(str(root / registry_prefix), mappings)
    changed = write_atomic(output, content)
    enabled = sum(len(sources) for sources in mappings.values())
    comment = (
        f"reactor registry compiled: discovered={discovered}, "
        f"enabled={enabled}, tags={len(mappings)}"
    )
    print(json.dumps({"changed": changed, "comment": comment}, ensure_ascii=False))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile /srv/salt Reactor mapping registries"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    compiler = subparsers.add_parser("compile")
    compiler.add_argument("--source", choices=("local", "fileserver"), default="local")
    compiler.add_argument("--root", required=True, type=Path)
    compiler.add_argument("--registry-prefix", default="reactor")
    compiler.add_argument("--output", required=True, type=Path)
    compiler.add_argument("--saltenv", default="base")
    compiler.add_argument("--backend", default="")
    compiler.add_argument("--salt-run", default="/opt/saltstack/salt/salt-run")
    compiler.add_argument("--salt-call", default="/opt/saltstack/salt/salt-call")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "compile":
            if args.source == "fileserver":
                compile_registry_fileserver(
                    args.output,
                    salt_run=args.salt_run,
                    salt_call=args.salt_call,
                    saltenv=args.saltenv,
                    backend=args.backend,
                    registry_prefix=args.registry_prefix,
                )
            else:
                compile_registry(args.root, args.output, args.registry_prefix)
        else:  # argparse prevents this path.
            fail(f"unknown command: {args.command}")
    except (OSError, RegistryError) as exc:
        print(f"reactor registry error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
