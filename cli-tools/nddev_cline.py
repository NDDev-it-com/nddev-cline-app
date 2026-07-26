#!/usr/bin/env python3
"""Transactional setup manager for an explicit Cline target."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
BUILDER_ROOT = ROOT / "plugins" / "nddev-builder"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-cline-app"
COMMAND_NAME = "cline"
STAMP_NAME = "NDDEV-CLINE-SETUP.json"
BACKUP_NAME = "NDDEV-CLINE-BACKUP.json"
BASELINE_REF = ROOT / "references" / "cline-baseline.json"
TESTED_CLI_VERSION = "3.0.46"
TESTED_EXTENSION_VERSION = "4.0.11"
NPM_PACKAGE = "cline"
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
OWNER_EXECUTABLE_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 120
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
MANAGED_SETTINGS_KEYS = (
    "autoApprove",
    "browser",
    "checkpoint",
    "cline",
    "commandPermissions",
    "commandExecution",
    "dangerousActions",
    "mcp",
    "mode",
    "network",
    "notifications",
    "privacy",
    "sandbox",
    "telemetry",
)
BUILDER_SOURCE_FILES = (
    (
        Path("skills") / "nddev-builder" / "SKILL.md",
        Path("data") / "settings" / "skills" / "nddev-builder" / "SKILL.md",
    ),
    (
        Path("skills") / "nddev-builder" / "SKILL.md",
        Path("skills") / "nddev-builder" / "SKILL.md",
    ),
    (
        Path("agents") / "nddev-builder.md",
        Path("agents") / "nddev-builder.md",
    ),
    (
        Path("plugins") / "nddev-builder" / "package.json",
        Path("plugins") / "nddev-builder" / "package.json",
    ),
    (
        Path("plugins") / "nddev-builder" / "index.js",
        Path("plugins") / "nddev-builder" / "index.js",
    ),
)
MANAGED_PATHS = (
    Path("data") / "settings" / "global-settings.json",
    Path("data") / "settings" / "cline_mcp_settings.json",
    Path("rules") / "nddev-managed.md",
    *(target for _, target in BUILDER_SOURCE_FILES),
    Path(STAMP_NAME),
)
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "canonical_target",
    "managed_files",
    "builder_projection",
    "launch_args",
    "command_permissions",
}
BACKUP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "slot",
    "canonical_target",
    "source_setup_id",
    "managed_files",
    "created_at",
}
TOKEN_ENV_NAMES = {
    "CLINE_API_KEY",
    "CLINE_AUTH_TOKEN",
    "CLINE_ACCOUNT_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
    "GIT_ASKPASS",
}


class ClineSetupError(Exception):
    """A safe user-facing lifecycle failure."""


class ConcurrentTargetChange(ClineSetupError):
    """A fail-closed target race."""


def fail(message: str) -> NoReturn:
    raise ClineSetupError(message)


def fail_concurrent(message: str) -> NoReturn:
    raise ConcurrentTargetChange(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def owner_of(info: os.stat_result) -> int | None:
    return info.st_uid if hasattr(info, "st_uid") else None


def is_owner_only_file(info: os.stat_result) -> bool:
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        return False
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        return False
    return True


def is_owner_private_directory(info: os.stat_result) -> bool:
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != OWNER_DIRECTORY_MODE:
        return False
    if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
        return False
    return True


def require_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    return info


def require_regular_file(path: Path, label: str, *, owner_only: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only and not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    if info.st_size > MANAGED_PAYLOAD_MAX_BYTES:
        fail(f"{label} exceeds the {MANAGED_PAYLOAD_MAX_BYTES}-byte size limit")
    return info


def read_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(path, label, owner_only=owner_only)
    if before.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was being opened")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            fail(f"{label} changed to an unsafe file")
        if owner_only and not is_owner_only_file(opened):
            fail(f"{label} must be owned by the current user with mode 0600")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label, owner_only=owner_only)
    expected = identity_of(before)
    if identity_of(after) != expected or identity_of(final) != expected:
        fail_concurrent(f"{label} changed while it was being read")
    return b"".join(chunks), final


def parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def load_json_object(path: Path, label: str, *, owner_only: bool = False) -> dict[str, Any]:
    content, _ = read_regular_file(path, label, owner_only=owner_only, max_bytes=METADATA_MAX_BYTES)
    return parse_json_object(content, label)


def require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        fail(
            f"{label} has invalid keys "
            f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
        )


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


def managed_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in MANAGED_SETTINGS_KEYS if key in settings}


def merge_settings(
    existing: dict[str, Any] | None, setup_settings: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if existing is not None:
        for key, value in existing.items():
            if key not in MANAGED_SETTINGS_KEYS:
                result[key] = value
    result.update(managed_settings_view(setup_settings))
    return result


def managed_digest(relative: Path, content: bytes) -> str:
    if relative == Path("data") / "settings" / "global-settings.json":
        settings = parse_json_object(content, str(relative))
        return sha256_bytes(canonical_json(managed_settings_view(settings)))
    return sha256_bytes(content)


def validate_command_permissions(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        fail(f"{label} command_permissions must be an object")
    allow = value.get("allow")
    deny = value.get("deny")
    if not isinstance(allow, list) or not all(isinstance(item, str) for item in allow):
        fail(f"{label} command_permissions.allow must be a string array")
    if not isinstance(deny, list) or not all(isinstance(item, str) for item in deny):
        fail(f"{label} command_permissions.deny must be a string array")
    if not isinstance(value.get("allowRedirects"), bool):
        fail(f"{label} command_permissions.allowRedirects must be boolean")


def validate_settings(settings: dict[str, Any], label: str) -> None:
    cline = settings.get("cline")
    sandbox = settings.get("sandbox")
    command = settings.get("commandExecution")
    if not isinstance(cline, dict) or cline.get("dataDir") != "${CLINE_DATA_DIR}":
        fail(f"{label} must bind cline.dataDir")
    if cline.get("autoUpdate") is not False:
        fail(f"{label} must disable automatic updates")
    if not isinstance(sandbox, dict) or not isinstance(sandbox.get("enabled"), bool):
        fail(f"{label} must define sandbox.enabled")
    if not isinstance(command, dict) or "autoApprove" not in command:
        fail(f"{label} must define commandExecution.autoApprove")
    validate_command_permissions(settings.get("commandPermissions"), f"{label}")
    if settings.get("telemetry") != {"enabled": False}:
        fail(f"{label} must disable telemetry")
    if settings.get("mcp") != {"servers": {}}:
        fail(f"{label} must not configure live MCP servers")
    if settings.get("cline", {}).get("plugins") != {"enabled": ["nddev-builder"]}:
        fail(f"{label} must enable nddev-builder plugin")


def validate_mcp_settings(settings: dict[str, Any], label: str) -> None:
    if settings != {"mcpServers": {}}:
        fail(f"{label} must not configure live MCP servers")


def validate_setup_metadata(metadata: dict[str, Any], setup_id: str) -> None:
    require_exact_keys(
        metadata,
        {
            "schema_version",
            "id",
            "description",
            "managed_files",
            "builder_projection",
            "builder_default_on",
            "launch_args",
        },
        f"setup {setup_id} metadata",
    )
    if metadata["schema_version"] != 1:
        fail(f"setup {setup_id} metadata has unsupported schema")
    if metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if metadata["managed_files"] != [
        "data/settings/global-settings.json",
        "data/settings/cline_mcp_settings.json",
        "rules/nddev-managed.md",
    ]:
        fail(f"setup {setup_id} managed file declaration is invalid")
    if (
        metadata["builder_projection"] != "native-skills-agents-plugin-user-files"
        or metadata["builder_default_on"] is not True
    ):
        fail(f"setup {setup_id} must enable native builder projection")
    if not isinstance(metadata["launch_args"], list) or not all(
        isinstance(item, str) for item in metadata["launch_args"]
    ):
        fail(f"setup {setup_id} launch_args must be a string array")


def render_builder_files() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for source_relative, target_relative in BUILDER_SOURCE_FILES:
        content, _ = read_regular_file(
            BUILDER_ROOT / source_relative, f"builder source {source_relative}"
        )
        files[target_relative] = content
    return files


def build_stamp(
    setup_id: str,
    desired: dict[Path, bytes],
    launch_args: list[str],
    command_permissions: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "canonical_target": "",
        "managed_files": {
            str(relative): managed_digest(relative, content)
            for relative, content in desired.items()
            if relative != Path(STAMP_NAME)
        },
        "builder_projection": "cline-native-skills-agents-plugin-user-files",
        "launch_args": launch_args,
        "command_permissions": command_permissions,
    }


def bind_stamp(stamp: dict[str, Any], canonical_target: Path) -> dict[str, Any]:
    bound = dict(stamp)
    bound["canonical_target"] = str(canonical_target)
    return bound


def render_setup(
    setup_id: str, *, existing_settings: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    validate_setup_id(setup_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")
    metadata = load_json_object(setup_root / "setup.json", f"setup {setup_id} metadata")
    validate_setup_metadata(metadata, setup_id)
    settings = load_json_object(
        setup_root / "global-settings.json", f"setup {setup_id}/global-settings.json"
    )
    validate_settings(settings, f"setup {setup_id}/global-settings.json")
    mcp_settings = load_json_object(
        setup_root / "cline_mcp_settings.json",
        f"setup {setup_id}/cline_mcp_settings.json",
    )
    validate_mcp_settings(mcp_settings, f"setup {setup_id}/cline_mcp_settings.json")
    rules_md, _ = read_regular_file(setup_root / "nddev-managed.md", f"setup {setup_id}/rules")
    merged_settings = merge_settings(existing_settings, settings)
    desired: dict[Path, bytes] = {
        Path("data") / "settings" / "global-settings.json": canonical_json(merged_settings),
        Path("data") / "settings" / "cline_mcp_settings.json": canonical_json(mcp_settings),
        Path("rules") / "nddev-managed.md": rules_md,
    }
    desired.update(render_builder_files())
    desired[Path(STAMP_NAME)] = canonical_json(
        build_stamp(
            setup_id,
            desired,
            metadata["launch_args"],
            merged_settings["commandPermissions"],
        )
    )
    return metadata, desired


def list_setups() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(CATALOG_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        metadata = load_json_object(path / "setup.json", f"setup {path.name} metadata")
        validate_setup_metadata(metadata, path.name)
        result.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "builder_default_on": metadata["builder_default_on"],
                "launch_args": metadata["launch_args"],
            }
        )
    return result


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-cline-backups"


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-cline.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, OWNER_DIRECTORY_MODE)
    except FileExistsError:
        fail(f"target is locked: {path}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.rmdir()


def require_explicit_absolute_target(raw_target: str | None) -> Path:
    if not raw_target:
        fail("an explicit --target absolute path is required")
    target = Path(raw_target).expanduser()
    if not target.is_absolute():
        fail("--target must be an absolute path")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(info.st_mode):
        fail("--target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("--target must be a directory")
    return target.resolve()


def ensure_target_directory(target: Path) -> Path:
    if target.exists():
        info = require_directory(target, "target")
        if not is_owner_private_directory(info):
            os.chmod(target, OWNER_DIRECTORY_MODE)
        return target.resolve()
    require_directory(target.parent, "target parent")
    target.mkdir(mode=OWNER_DIRECTORY_MODE)
    return target.resolve()


def ensure_private_parent(target: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        fail(f"unsafe managed path: {relative}")
    current = target
    for part in relative.parent.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                fail(f"managed parent {current.relative_to(target)} must be a real directory")
            if not is_owner_private_directory(info):
                os.chmod(current, OWNER_DIRECTORY_MODE)
        else:
            current.mkdir(mode=OWNER_DIRECTORY_MODE)
    return target / relative


def any_managed_path_exists(target: Path) -> bool:
    return any(
        (target / relative).exists() or (target / relative).is_symlink()
        for relative in MANAGED_PATHS
    )


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp = target / STAMP_NAME
    if not stamp.exists() and not stamp.is_symlink():
        return None
    value = load_json_object(stamp, "setup stamp", owner_only=True)
    require_exact_keys(value, STAMP_KEYS, "setup stamp")
    if (
        value["schema_version"] != 1
        or value["product_name"] != PRODUCT_NAME
        or value["build_version"] != VERSION
    ):
        fail("setup stamp is not compatible with this build")
    if value["canonical_target"] != str(target):
        fail("setup stamp is bound to a different canonical target")
    if not isinstance(value["managed_files"], dict):
        fail("setup stamp managed_files must be an object")
    validate_setup_id(value["setup_id"])
    return value


def validate_managed_files(target: Path, stamp: dict[str, Any]) -> list[str]:
    expected = stamp["managed_files"]
    ordered = [relative for relative in MANAGED_PATHS if str(relative) in expected]
    ordered.extend(Path(raw) for raw in sorted(set(expected) - {str(item) for item in ordered}))
    drift: list[str] = []
    for relative in ordered:
        if relative.is_absolute() or ".." in relative.parts:
            fail("setup stamp contains an unsafe managed path")
        content, _ = read_regular_file(
            target / relative, f"managed file {relative}", owner_only=True
        )
        if managed_digest(relative, content) != expected[str(relative)]:
            drift.append(str(relative))
    if drift:
        fail(f"managed target drift detected: {', '.join(sorted(drift))}")
    return sorted(expected)


def inspect_target(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {"state": "missing", "target": str(target)}
    require_directory(target, "target")
    stamp = load_stamp(target)
    if stamp is None:
        if any_managed_path_exists(target):
            fail("unmanaged target contains nddev-managed paths")
        return {"state": "unmanaged", "target": str(target)}
    return {
        "state": "managed",
        "target": str(target),
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "managed_files": validate_managed_files(target, stamp),
        "builder_projection": stamp["builder_projection"],
        "launch_args": stamp["launch_args"],
        "command_permissions": stamp["command_permissions"],
    }


def read_existing_settings_if_managed(target: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("state") != "managed":
        return None
    return load_json_object(
        target / "data" / "settings" / "global-settings.json",
        "existing data/settings/global-settings.json",
        owner_only=True,
    )


def current_managed_snapshot(target: Path) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for relative in MANAGED_PATHS:
        path = target / relative
        if path.exists() or path.is_symlink():
            content, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
            snapshot[relative] = content
        else:
            snapshot[relative] = None
    return snapshot


def prune_empty_managed_dirs(target: Path) -> None:
    candidates = sorted(
        {(target / relative).parent for relative in MANAGED_PATHS},
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in candidates:
        while directory != target and directory.is_dir() and not directory.is_symlink():
            try:
                directory.rmdir()
            except OSError:
                break
            directory = directory.parent


def restore_snapshot(target: Path, snapshot: dict[Path, bytes | None]) -> None:
    for relative in sorted(MANAGED_PATHS, key=lambda item: len(item.parts), reverse=True):
        path = target / relative
        if path.exists() or path.is_symlink():
            path.unlink()
    for relative, content in snapshot.items():
        if content is None:
            continue
        path = ensure_private_parent(target, relative)
        path.write_bytes(content)
        path.chmod(OWNER_FILE_MODE)
    prune_empty_managed_dirs(target)


def replace_managed_state(
    target: Path, desired: dict[Path, bytes | None], expected: dict[str, Any]
) -> None:
    del expected
    for relative, content in desired.items():
        path = ensure_private_parent(target, relative)
        if content is None:
            if path.exists() or path.is_symlink():
                require_regular_file(path, f"managed file {relative}", owner_only=True)
                path.unlink()
            continue
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.chmod(OWNER_FILE_MODE)
        os.replace(temporary, path)
        path.chmod(OWNER_FILE_MODE)
    prune_empty_managed_dirs(target)


def changed_paths(target: Path, desired: dict[Path, bytes | None]) -> list[str]:
    changed: list[str] = []
    for relative, content in desired.items():
        path = target / relative
        if content is None:
            if path.exists() or path.is_symlink():
                changed.append(str(relative))
            continue
        if not path.exists() or path.is_symlink():
            changed.append(str(relative))
            continue
        actual, _ = read_regular_file(path, f"managed file {relative}", owner_only=True)
        if actual != content:
            changed.append(str(relative))
    return sorted(changed)


def refresh_backup_slot_numbers(pool: Path) -> None:
    for slot in range(10):
        envelope_path = pool / str(slot) / BACKUP_NAME
        if not envelope_path.exists():
            continue
        envelope = load_json_object(envelope_path, f"backup slot {slot} envelope", owner_only=True)
        envelope["slot"] = slot
        envelope_path.write_bytes(canonical_json(envelope))
        envelope_path.chmod(OWNER_FILE_MODE)


def create_backup(target: Path, state: dict[str, Any]) -> int:
    pool = backup_pool(target)
    pool.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    for slot in range(9, -1, -1):
        current = pool / str(slot)
        if not current.exists():
            continue
        if slot == 9:
            shutil.rmtree(current)
        else:
            os.replace(current, pool / str(slot + 1))
    slot_dir = pool / "0"
    slot_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    managed_files = list(state["managed_files"])
    for raw_relative in [*managed_files, STAMP_NAME]:
        relative = Path(raw_relative)
        content, _ = read_regular_file(
            target / relative, f"managed file {relative}", owner_only=True
        )
        destination = slot_dir / relative
        destination.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(OWNER_FILE_MODE)
    envelope = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": 0,
        "canonical_target": str(target),
        "source_setup_id": state["setup_id"],
        "managed_files": managed_files,
        "created_at": int(time.time()),
    }
    (slot_dir / BACKUP_NAME).write_bytes(canonical_json(envelope))
    (slot_dir / BACKUP_NAME).chmod(OWNER_FILE_MODE)
    refresh_backup_slot_numbers(pool)
    return 0


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[Path, bytes]]:
    if slot < 0 or slot > 9:
        fail("--backup must be between 0 and 9")
    slot_dir = backup_pool(target) / str(slot)
    require_directory(slot_dir, f"backup slot {slot}")
    envelope = load_json_object(slot_dir / BACKUP_NAME, "backup envelope", owner_only=True)
    require_exact_keys(envelope, BACKUP_KEYS, "backup envelope")
    if (
        envelope["schema_version"] != 1
        or envelope["product_name"] != PRODUCT_NAME
        or envelope["build_version"] != VERSION
    ):
        fail("backup envelope is not compatible with this build")
    if envelope["canonical_target"] != str(target):
        fail("backup belongs to a different canonical target")
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = Path(raw_relative)
        content, _ = read_regular_file(
            slot_dir / relative, f"backup file {relative}", owner_only=True
        )
        files[relative] = content
    return envelope, files


def mutate_setup(target: Path, setup_id: str, operation: str) -> dict[str, Any]:
    canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        existing_settings = read_existing_settings_if_managed(canonical_target, state)
        metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        backup_slot: int | None = None
        snapshot = current_managed_snapshot(canonical_target)
        try:
            if state["state"] == "managed" and changed:
                backup_slot = create_backup(canonical_target, state)
            if changed:
                replace_managed_state(canonical_target, desired, stamp)
            post = inspect_target(canonical_target)
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": operation,
        "setup_id": setup_id,
        "description": metadata["description"],
        "target": str(canonical_target),
        "changed": changed,
        "backup_slot": backup_slot,
        "state": post["state"],
    }


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    canonical_target = target.resolve() if target.exists() else target
    state = inspect_target(canonical_target)
    existing_settings = read_existing_settings_if_managed(canonical_target, state)
    _metadata, desired = render_setup(setup_id, existing_settings=existing_settings)
    if state["state"] == "managed":
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        operation = "switch" if state.get("setup_id") != setup_id else "install"
        backup_required = bool(changed)
    else:
        changed = sorted(str(path) for path in desired)
        operation = "install"
        backup_required = False
    return {
        "ok": True,
        "operation": operation,
        "setup_id": setup_id,
        "target": str(canonical_target),
        "state": state["state"],
        "mutates": False,
        "backup_required": backup_required,
        "changed": changed,
    }


def remove_setup(target: Path) -> dict[str, Any]:
    canonical_target = require_explicit_absolute_target(str(target))
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-cline-app")
        snapshot = current_managed_snapshot(canonical_target)
        desired = {relative: None for relative in MANAGED_PATHS}
        try:
            backup_slot = create_backup(canonical_target, state)
            replace_managed_state(canonical_target, desired, {})
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": "remove",
        "target": str(canonical_target),
        "removed_setup_id": state["setup_id"],
        "backup_slot": backup_slot,
    }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    canonical_target = require_explicit_absolute_target(str(target))
    with target_lock(canonical_target):
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-cline-app")
        envelope, desired = load_backup(canonical_target, slot)
        snapshot = current_managed_snapshot(canonical_target)
        try:
            replace_managed_state(canonical_target, desired, {})
            post = inspect_target(canonical_target)
        except BaseException:
            restore_snapshot(canonical_target, snapshot)
            raise
    return {
        "ok": True,
        "operation": "restore",
        "target": str(canonical_target),
        "setup_id": post["setup_id"],
        "restored_from_slot": slot,
        "restored_source_setup_id": envelope["source_setup_id"],
    }


def load_baseline() -> dict[str, Any]:
    return load_json_object(BASELINE_REF, "Cline baseline")


def software_manifest_path(target: Path) -> Path:
    return target / "software" / "cline-cli.json"


def cline_executable(target: Path) -> Path:
    suffix = ".cmd" if sys.platform.startswith("win") else ""
    return target / "bin" / f"{COMMAND_NAME}{suffix}"


def software_status(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(target),
            "version": None,
            "executable": None,
            "extension_supported": False,
            "extension_installed": None,
        }
    canonical_target = require_explicit_absolute_target(str(target))
    executable = cline_executable(canonical_target)
    manifest = software_manifest_path(canonical_target)
    if not executable.exists() or not manifest.exists():
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(canonical_target),
            "version": None,
            "executable": str(executable),
            "extension_supported": False,
            "extension_installed": None,
        }
    require_regular_file(executable, "Cline CLI executable")
    info = load_json_object(manifest, "software manifest", owner_only=True)
    current = (
        info.get("schema_version") == 1
        and info.get("version") == TESTED_CLI_VERSION
        and info.get("executable") == f"bin/{COMMAND_NAME}"
    )
    return {
        "ok": True,
        "installed": True,
        "current": current,
        "target": str(canonical_target),
        "version": info.get("version"),
        "executable": str(executable),
        "extension_supported": False,
        "extension_installed": None,
    }


def download_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": PRODUCT_NAME})
    with urllib.request.urlopen(request, timeout=PROCESS_TIMEOUT_SECONDS) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > DOWNLOAD_MAX_BYTES:
                fail("Cline CLI download exceeded the 256 MiB bound")
            chunks.append(chunk)
    return b"".join(chunks)


def expected_integrity_digest(integrity: str) -> bytes:
    algorithm, encoded = integrity.split("-", 1)
    if algorithm != "sha512":
        fail("only sha512 npm integrity is supported")
    return base64.b64decode(encoded)


def validate_npm_integrity(content: bytes, integrity: str) -> None:
    expected = expected_integrity_digest(integrity)
    actual = hashlib.sha512(content).digest()
    if actual != expected:
        fail("downloaded Cline CLI npm tarball integrity mismatch")


def safe_tar_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            fail(f"unsafe archive member path: {member.name}")
        if member.issym() or member.islnk() or member.isdev():
            fail(f"unsafe archive member type: {member.name}")
    return members


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        baseline = load_baseline()
        npm = baseline.get("npm")
        if not isinstance(npm, dict):
            fail("baseline npm metadata missing")
        url = npm.get("tarball")
        integrity = npm.get("integrity")
        if not isinstance(url, str) or not isinstance(integrity, str):
            fail("baseline npm tarball metadata is incomplete")
        archive_bytes = download_bytes(url)
        validate_npm_integrity(archive_bytes, integrity)
        staging = canonical_target.parent / f".{canonical_target.name}.nddev-cline-cli-stage"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=OWNER_DIRECTORY_MODE)
        try:
            archive_path = staging / "cline.tgz"
            archive_path.write_bytes(archive_bytes)
            unpacked = staging / "unpacked"
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(unpacked, members=safe_tar_members(archive))
            binary_source = unpacked / "package" / "bin" / "cline"
            if not binary_source.is_file():
                fail("npm tarball did not contain package/bin/cline")
            bin_dir = canonical_target / "bin"
            bin_dir.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
            executable = cline_executable(canonical_target)
            temporary = bin_dir / f".{executable.name}.tmp"
            shutil.copy2(binary_source, temporary)
            temporary.chmod(OWNER_EXECUTABLE_MODE)
            os.replace(temporary, executable)
            manifest = {
                "schema_version": 1,
                "version": TESTED_CLI_VERSION,
                "executable": f"bin/{COMMAND_NAME}",
                "package": NPM_PACKAGE,
                "integrity": integrity,
                "tarball": url,
            }
            software_manifest_path(canonical_target).parent.mkdir(
                mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True
            )
            software_manifest_path(canonical_target).write_bytes(canonical_json(manifest))
            software_manifest_path(canonical_target).chmod(OWNER_FILE_MODE)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "operation": operation,
        "target": str(canonical_target),
        "version": TESTED_CLI_VERSION,
        "package": NPM_PACKAGE,
        "executable": str(cline_executable(canonical_target)),
    }


def isolated_child_environment(target: Path, command_permissions: dict[str, Any]) -> dict[str, str]:
    home = target / "home"
    data = target / "data"
    sandbox = target / "sandbox"
    runtime = target / "runtime"
    tmp = runtime / "tmp"
    for directory in (home, data, sandbox, runtime, tmp):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name in TOKEN_ENV_NAMES:
            continue
        if name.startswith("CLINE_"):
            continue
        env[name] = value
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CLINE_DATA_DIR": str(data),
            "CLINE_SANDBOX": "true",
            "CLINE_SANDBOX_DATA_DIR": str(sandbox),
            "CLINE_HOOKS_DIR": str(target / "hooks"),
            "CLINE_SESSION_BACKEND_MODE": "local",
            "CLINE_COMMAND_PERMISSIONS": json.dumps(command_permissions, sort_keys=True),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
            "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
            "XDG_STATE_HOME": str(runtime / "xdg-state"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
    )
    return env


def launch_cline(target: Path, args: list[str]) -> int:
    canonical_target = require_explicit_absolute_target(str(target))
    state = inspect_target(canonical_target)
    if state["state"] != "managed":
        fail("target is not managed by nddev-cline-app")
    status = software_status(canonical_target)
    if not status["installed"] or not status["current"]:
        fail("Cline CLI is not installed at the tested version in this target")
    child_args = [
        *state["launch_args"],
        "--data-dir",
        str(canonical_target),
        "--config",
        str(canonical_target / "data" / "settings"),
        "--hooks-dir",
        str(canonical_target / "hooks"),
        *args,
    ]
    completed = subprocess.run(
        [str(cline_executable(canonical_target)), *child_args],
        cwd=os.getcwd(),
        env=isolated_child_environment(canonical_target, state["command_permissions"]),
        check=False,
        timeout=None,
    )
    return int(completed.returncode)


def print_payload(payload: dict[str, Any], *, json_output: bool) -> None:
    del json_output
    print(json.dumps(payload, indent=2, sort_keys=True))


def add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print JSON output")


def add_target_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="explicit absolute Cline target")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list setup variants")
    add_json_argument(list_parser)
    for name in ("status", "software-status"):
        command_parser = subparsers.add_parser(name, help=f"{name} for a target")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    for name in ("plan", "install", "switch"):
        command_parser = subparsers.add_parser(name, help=f"{name} a setup")
        command_parser.add_argument("--setup", required=True)
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    restore_parser = subparsers.add_parser("restore", help="restore a target-bound backup")
    restore_parser.add_argument("--backup", type=int, required=True)
    add_target_argument(restore_parser)
    add_json_argument(restore_parser)
    remove_parser = subparsers.add_parser("remove", help="remove nddev-managed setup files")
    add_target_argument(remove_parser)
    add_json_argument(remove_parser)
    for name in ("install-cli", "update-cli"):
        command_parser = subparsers.add_parser(name, help=f"{name} exact tested Cline CLI")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    launch_parser = subparsers.add_parser("launch", help="launch target-owned Cline CLI")
    add_target_argument(launch_parser)
    add_json_argument(launch_parser)
    launch_parser.add_argument("cline_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def error_result(message: str, *, json_output: bool) -> int:
    if json_output:
        print(json.dumps({"ok": False, "error": message}, indent=2, sort_keys=True))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    if args.command == "list":
        print_payload({"ok": True, "setups": list_setups()}, json_output=args.json)
        return 0
    if args.command == "status":
        target = require_explicit_absolute_target(args.target)
        print_payload({"ok": True, **inspect_target(target)}, json_output=args.json)
        return 0
    if args.command == "software-status":
        target = require_explicit_absolute_target(args.target)
        print_payload(software_status(target), json_output=args.json)
        return 0
    if args.command == "plan":
        target = require_explicit_absolute_target(args.target)
        print_payload(plan_setup(target, args.setup), json_output=args.json)
        return 0
    if args.command in {"install", "switch"}:
        target = require_explicit_absolute_target(args.target)
        print_payload(mutate_setup(target, args.setup, args.command), json_output=args.json)
        return 0
    if args.command == "restore":
        target = require_explicit_absolute_target(args.target)
        print_payload(restore_backup(target, args.backup), json_output=args.json)
        return 0
    if args.command == "remove":
        target = require_explicit_absolute_target(args.target)
        print_payload(remove_setup(target), json_output=args.json)
        return 0
    if args.command == "install-cli":
        target = require_explicit_absolute_target(args.target)
        print_payload(install_or_update_cli(target, operation="install-cli"), json_output=args.json)
        return 0
    if args.command == "update-cli":
        target = require_explicit_absolute_target(args.target)
        print_payload(install_or_update_cli(target, operation="update-cli"), json_output=args.json)
        return 0
    if args.command == "launch":
        target = require_explicit_absolute_target(args.target)
        cline_args = list(args.cline_args)
        if cline_args and cline_args[0] == "--":
            cline_args = cline_args[1:]
        return launch_cline(target, cline_args)
    fail(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        return run(args)
    except ClineSetupError as exc:
        json_output = "--json" in (argv if argv is not None else sys.argv[1:])
        return error_result(str(exc), json_output=json_output)


if __name__ == "__main__":
    raise SystemExit(main())
