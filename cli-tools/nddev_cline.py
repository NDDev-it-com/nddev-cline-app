#!/usr/bin/env python3
"""Transactional setup manager for an explicit Cline target."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
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
BACKUP_POOL_NAME = "NDDEV-CLINE-BACKUPS.json"
BACKUP_NAME = "NDDEV-CLINE-BACKUP.json"
BASELINE_REF = ROOT / "references" / "cline-baseline.json"
TESTED_CLI_VERSION = "3.0.46"
TESTED_EXTENSION_VERSION = "4.0.11"
NPM_PACKAGE = "cline"
BUN_PACKAGE_SPEC = f"{NPM_PACKAGE}@{TESTED_CLI_VERSION}"
BUN_INSTALL_ARGV = ("add", "--global", "--exact", "--trust", BUN_PACKAGE_SPEC)
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
OWNER_EXECUTABLE_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
SOFTWARE_TREE_MAX_BYTES = 512 * 1024 * 1024
# Keep this limit synchronized with build/manifest.json software_lifecycle.bounds.
SOFTWARE_TREE_MAX_PATHS = 50000
PROCESS_OUTPUT_MAX_BYTES = 64 * 1024
PROCESS_TIMEOUT_SECONDS = 120
# Fresh cline@3.0.46 macOS arm64 stages measured 15.214s at the slowest.
# references/cline-baseline.json owns the calibration evidence and public bound.
VERSION_PROBE_TIMEOUT_SECONDS = 60
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_PATTERN = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
SOFTWARE_DIR_RELATIVE = Path("software") / "cline-cli"
SOFTWARE_MANIFEST_RELATIVE = Path("software") / "cline-cli.json"
SOFTWARE_REPLACE_PATHS = (
    Path("bin") / COMMAND_NAME,
    SOFTWARE_DIR_RELATIVE,
    SOFTWARE_MANIFEST_RELATIVE,
)
SOFTWARE_PARENT_PATHS = tuple(
    sorted(
        {relative.parent for relative in SOFTWARE_REPLACE_PATHS if relative.parent != Path(".")},
        key=str,
    )
)
PACKAGE_WRAPPER_RELATIVE = (
    SOFTWARE_DIR_RELATIVE / "install" / "global" / "node_modules" / "cline" / "bin" / "cline"
)
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
BACKUP_POOL_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "canonical_target",
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
BLOCKED_LAUNCH_FLAGS = {
    "--auto-approve",
    "--config",
    "--data-dir",
    "--hooks-dir",
    "--key",
    "-k",
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


def require_private_directory(path: Path, label: str) -> os.stat_result:
    info = require_directory(path, label)
    if not is_owner_private_directory(info):
        fail(f"{label} must be owned by the current user with mode 0700")
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


def backup_pool_marker(pool: Path) -> Path:
    return pool / BACKUP_POOL_NAME


def lock_path(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-cline.lock"


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    require_private_directory(path.parent, "target lock parent")
    created = False
    try:
        os.mkdir(path, OWNER_DIRECTORY_MODE)
    except FileExistsError:
        fail(f"target is locked: {path}")
    except OSError as exc:
        fail(f"cannot create target lock {path}: {exc}")
    created = True
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            if created:
                require_private_directory(path, "target lock")
            path.rmdir()


def require_explicit_absolute_target(raw_target: str | None) -> Path:
    if not raw_target:
        fail("an explicit --target absolute path is required")
    target = Path(raw_target)
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
    try:
        info = target.lstat()
    except FileNotFoundError:
        require_private_directory(target.parent, "target parent")
        target.mkdir(mode=OWNER_DIRECTORY_MODE)
        target.chmod(OWNER_DIRECTORY_MODE)
        return target.resolve()
    if stat.S_ISLNK(info.st_mode):
        fail("target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a directory")
    if not is_owner_private_directory(info):
        fail("target must be owned by the current user with mode 0700")
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
                fail(
                    f"managed parent {current.relative_to(target)} "
                    "must be owned by the current user with mode 0700"
                )
        else:
            current.mkdir(mode=OWNER_DIRECTORY_MODE)
            current.chmod(OWNER_DIRECTORY_MODE)
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
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {"state": "missing", "target": str(target)}
    if stat.S_ISLNK(info.st_mode):
        fail("target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a directory")
    if not is_owner_private_directory(info):
        fail("target must be owned by the current user with mode 0700")
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


def validate_backup_pool_marker(target: Path, pool: Path) -> None:
    marker = load_json_object(
        backup_pool_marker(pool),
        "backup pool marker",
        owner_only=True,
    )
    require_exact_keys(marker, BACKUP_POOL_KEYS, "backup pool marker")
    if (
        marker["schema_version"] != 1
        or marker["product_name"] != PRODUCT_NAME
        or marker["build_version"] != VERSION
    ):
        fail("backup pool marker is not compatible with this build")
    if marker["canonical_target"] != str(target):
        fail("backup pool is bound to a different canonical target")


def write_backup_pool_marker(target: Path, pool: Path) -> None:
    marker = {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
    }
    marker_path = backup_pool_marker(pool)
    marker_path.write_bytes(canonical_json(marker))
    marker_path.chmod(OWNER_FILE_MODE)


def require_backup_pool(target: Path) -> Path:
    pool = backup_pool(target)
    require_private_directory(pool, "backup pool")
    validate_backup_pool_marker(target, pool)
    return pool


def ensure_backup_pool(target: Path) -> Path:
    pool = backup_pool(target)
    try:
        info = pool.lstat()
    except FileNotFoundError:
        require_private_directory(target.parent, "backup pool parent")
        pool.mkdir(mode=OWNER_DIRECTORY_MODE)
        pool.chmod(OWNER_DIRECTORY_MODE)
        write_backup_pool_marker(target, pool)
        return pool
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("backup pool must be a real directory")
    if not is_owner_private_directory(info):
        fail("backup pool must be owned by the current user with mode 0700")
    validate_backup_pool_marker(target, pool)
    return pool


def validate_backup_envelope(
    target: Path,
    envelope: dict[str, Any],
    label: str,
    *,
    expected_slot: int | None,
) -> None:
    require_exact_keys(envelope, BACKUP_KEYS, label)
    if (
        envelope["schema_version"] != 1
        or envelope["product_name"] != PRODUCT_NAME
        or envelope["build_version"] != VERSION
    ):
        fail(f"{label} is not compatible with this build")
    slot = envelope["slot"]
    if not isinstance(slot, int) or slot < 0 or slot > 9:
        fail(f"{label} slot is invalid")
    if expected_slot is not None and slot != expected_slot:
        fail(f"{label} slot identity mismatch")
    if envelope["canonical_target"] != str(target):
        fail("backup belongs to a different canonical target")
    if not isinstance(envelope["source_setup_id"], str):
        fail(f"{label} source_setup_id must be a string")
    validate_setup_id(envelope["source_setup_id"])
    if not isinstance(envelope["created_at"], int):
        fail(f"{label} created_at must be an integer")
    raw_files = envelope["managed_files"]
    if not isinstance(raw_files, list) or not all(isinstance(item, str) for item in raw_files):
        fail(f"{label} managed_files must be a string array")
    if len(raw_files) != len(set(raw_files)):
        fail(f"{label} managed_files must be unique")
    for raw_relative in raw_files:
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts or relative == Path(STAMP_NAME):
            fail(f"{label} contains an unsafe managed file path")


def load_backup_envelope(
    target: Path,
    slot_dir: Path,
    slot: int,
    *,
    expected_slot: int | None,
) -> dict[str, Any]:
    require_private_directory(slot_dir, f"backup slot {slot}")
    envelope = load_json_object(
        slot_dir / BACKUP_NAME,
        f"backup slot {slot} envelope",
        owner_only=True,
    )
    validate_backup_envelope(
        target,
        envelope,
        f"backup slot {slot} envelope",
        expected_slot=expected_slot,
    )
    return envelope


def backup_slots_for_rotation(target: Path, pool: Path) -> list[int]:
    slots: list[int] = []
    for child in pool.iterdir():
        if child.name == BACKUP_POOL_NAME:
            continue
        if not child.name.isdigit():
            fail("backup pool contains an unmanaged path")
        slot = int(child.name)
        if slot < 0 or slot > 9:
            fail("backup pool contains a slot outside the 0-9 rotation window")
        load_backup_envelope(target, child, slot, expected_slot=slot)
        slots.append(slot)
    return sorted(set(slots))


def refresh_backup_slot_numbers(target: Path, pool: Path) -> None:
    for slot in range(10):
        slot_dir = pool / str(slot)
        if not path_present(slot_dir):
            continue
        envelope = load_backup_envelope(target, slot_dir, slot, expected_slot=None)
        envelope["slot"] = slot
        envelope_path = slot_dir / BACKUP_NAME
        envelope_path.write_bytes(canonical_json(envelope))
        envelope_path.chmod(OWNER_FILE_MODE)


def create_backup(target: Path, state: dict[str, Any]) -> int:
    pool = ensure_backup_pool(target)
    for slot in sorted(backup_slots_for_rotation(target, pool), reverse=True):
        current = pool / str(slot)
        if slot == 9:
            # The slot was just validated as a target-bound manager backup.
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
        destination = ensure_private_parent(slot_dir, relative)
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
    refresh_backup_slot_numbers(target, pool)
    return 0


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[Path, bytes]]:
    if slot < 0 or slot > 9:
        fail("--backup must be between 0 and 9")
    pool = require_backup_pool(target)
    slot_dir = pool / str(slot)
    envelope = load_backup_envelope(target, slot_dir, slot, expected_slot=slot)
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts:
            fail("backup contains an unsafe managed file path")
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
    try:
        info = target.lstat()
    except FileNotFoundError:
        canonical_target = target
    else:
        if stat.S_ISLNK(info.st_mode):
            fail("target must not be a symlink")
        canonical_target = target.resolve()
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
    return target / SOFTWARE_MANIFEST_RELATIVE


def cline_executable(target: Path) -> Path:
    suffix = ".cmd" if sys.platform.startswith("win") else ""
    return target / "bin" / f"{COMMAND_NAME}{suffix}"


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_child_base_environment() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "ComSpec"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def run_bounded_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    label: str,
    timeout: int = PROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        fail(f"{label} executable was not found")
    except subprocess.TimeoutExpired:
        fail(f"{label} timed out after {timeout} seconds")
    output_size = len(completed.stdout.encode("utf-8", errors="replace")) + len(
        completed.stderr.encode("utf-8", errors="replace")
    )
    if output_size > PROCESS_OUTPUT_MAX_BYTES:
        fail(f"{label} exceeded the {PROCESS_OUTPUT_MAX_BYTES}-byte output limit")
    return completed


def isolated_probe_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    data = root / "data"
    tmp = root / "tmp"
    xdg_config = root / "xdg-config"
    xdg_cache = root / "xdg-cache"
    xdg_state = root / "xdg-state"
    sandbox = root / "sandbox"
    for directory in (home, data, tmp, xdg_config, xdg_cache, xdg_state, sandbox):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    env = safe_child_base_environment()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CLINE_DATA_DIR": str(data),
            "CLINE_SANDBOX": "true",
            "CLINE_SANDBOX_DATA_DIR": str(sandbox),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
        }
    )
    return env


def require_safe_executable(
    path: Path,
    target: Path | None,
    label: str,
    *,
    allow_hardlinks: bool = False,
    owner_only_mode: bool = True,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode):
        if target is None:
            fail(f"{label} must not be a symlink")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            fail(f"{label} symlink is broken")
        canonical_target = target.resolve()
        if not path_is_relative_to(resolved, canonical_target):
            fail(f"{label} symlink must stay inside the target")
        target_info = require_regular_file(
            resolved, f"{label} symlink target", owner_only=False
        )
        if not allow_hardlinks and target_info.st_nlink != 1:
            fail(f"{label} symlink target must not have hard-link aliases")
        if owner_only_mode and stat.S_IMODE(target_info.st_mode) & 0o077:
            fail(f"{label} symlink target must not be readable or writable by group/other")
        if not stat.S_IMODE(target_info.st_mode) & stat.S_IXUSR:
            fail(f"{label} symlink target must be executable by the owner")
        return target_info
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file or target-owned symlink")
    if not allow_hardlinks and info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only_mode and stat.S_IMODE(info.st_mode) & 0o077:
        fail(f"{label} must not be readable or writable by group/other")
    if not stat.S_IMODE(info.st_mode) & stat.S_IXUSR:
        fail(f"{label} must be executable by the owner")
    return info


def resolve_target_owned_path(path: Path, root: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if not stat.S_ISLNK(info.st_mode):
        return path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        fail(f"{label} symlink is broken")
    if not path_is_relative_to(resolved, root.resolve()):
        fail(f"{label} symlink must stay inside the target")
    return resolved


def observed_cline_version(executable: Path, *, target: Path | None = None) -> str:
    require_safe_executable(
        executable, target, "Cline CLI executable", allow_hardlinks=True
    )
    with tempfile.TemporaryDirectory(prefix="nddev-cline-version-") as temporary:
        root = Path(temporary)
        completed = run_bounded_process(
            [str(executable), "--version"],
            cwd=root,
            env=isolated_probe_environment(root),
            label="Cline CLI version probe",
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    if completed.returncode != 0:
        fail("Cline CLI version probe failed")
    combined = "\n".join((completed.stdout, completed.stderr))
    match = VERSION_PATTERN.search(combined)
    if match is None:
        fail("Cline CLI version probe did not return a SemVer version")
    return match.group(1)


def software_manifest_identity(
    baseline: dict[str, Any] | None = None, *, version: str = TESTED_CLI_VERSION
) -> dict[str, Any]:
    if baseline is None:
        baseline = load_baseline()
    npm = baseline.get("npm")
    if not isinstance(npm, dict):
        fail("baseline npm metadata missing")
    integrity = npm.get("integrity")
    shasum = npm.get("shasum")
    if not isinstance(integrity, str) or not isinstance(shasum, str):
        fail("baseline npm package metadata is incomplete")
    return {
        "schema_version": 1,
        "install_method": "bun-global",
        "package_manager": "bun",
        "package": NPM_PACKAGE,
        "package_spec": f"{NPM_PACKAGE}@{version}",
        "version": version,
        "executable": f"bin/{COMMAND_NAME}",
        "global_dir": str(SOFTWARE_DIR_RELATIVE / "install" / "global"),
        "bin_dir": "bin",
        "integrity": integrity,
        "shasum": shasum,
    }


def digest_regular_file(
    path: Path,
    label: str,
    byte_counter: dict[str, int],
    *,
    allow_hardlinks: bool = False,
) -> str:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        fail(f"{label} must be a regular file")
    if not allow_hardlinks and before.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            fail_concurrent(f"{label} changed while it was being opened")
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            byte_counter["value"] += len(chunk)
            if byte_counter["value"] > SOFTWARE_TREE_MAX_BYTES:
                fail(f"installed Cline CLI tree exceeds the {SOFTWARE_TREE_MAX_BYTES}-byte limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = path.lstat()
    expected = identity_of(before)
    if (
        identity_of(opened) != expected
        or identity_of(after) != expected
        or identity_of(final) != expected
    ):
        fail_concurrent(f"{label} changed while it was being read")
    return digest.hexdigest()


def iter_software_tree_paths(root: Path) -> list[Path]:
    paths = [Path("bin") / COMMAND_NAME]
    install_root = root / SOFTWARE_DIR_RELATIVE
    if install_root.exists() or install_root.is_symlink():
        paths.append(SOFTWARE_DIR_RELATIVE)
        for path in install_root.rglob("*"):
            paths.append(path.relative_to(root))
            if len(paths) > SOFTWARE_TREE_MAX_PATHS:
                fail(f"installed Cline CLI tree exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
    return sorted(set(paths), key=lambda item: str(item))


def compute_software_tree_digest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    byte_counter = {"value": 0}
    records: list[dict[str, Any]] = []
    for relative in iter_software_tree_paths(root):
        if len(records) >= SOFTWARE_TREE_MAX_PATHS:
            fail(f"installed Cline CLI tree exceeds the {SOFTWARE_TREE_MAX_PATHS}-path limit")
        path = root / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            fail(f"installed software path {relative} is missing")
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            if stat.S_ISLNK(info.st_mode):
                fail(f"installed software path {relative} must not be a directory symlink")
            records.append({"path": str(relative), "type": "directory", "mode": mode})
            continue
        if stat.S_ISLNK(info.st_mode):
            resolved = resolve_target_owned_path(path, root, f"installed software path {relative}")
            records.append(
                {
                    "path": str(relative),
                    "type": "symlink",
                    "target": os.readlink(path),
                    "resolved": str(resolved.relative_to(root)),
                }
            )
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"installed software path {relative} has an unsafe type")
        digest = digest_regular_file(
            path,
            f"installed software file {relative}",
            byte_counter,
            allow_hardlinks=True,
        )
        records.append(
            {
                "path": str(relative),
                "type": "file",
                "mode": mode,
                "size": info.st_size,
                "nlink": info.st_nlink,
                "sha256": digest,
                "owner_executable": bool(mode & stat.S_IXUSR),
            }
        )
    require_safe_executable(
        root / "bin" / COMMAND_NAME,
        root,
        "Cline CLI executable",
        allow_hardlinks=True,
    )
    entrypoint = resolve_target_owned_path(root / "bin" / COMMAND_NAME, root, "Cline CLI executable")
    entrypoint_sha256 = digest_regular_file(
        entrypoint, "Cline CLI executable", {"value": 0}, allow_hardlinks=True
    )
    package_wrapper = root / PACKAGE_WRAPPER_RELATIVE
    package_wrapper_sha256 = digest_regular_file(
        package_wrapper, "Cline package wrapper", {"value": 0}, allow_hardlinks=False
    )
    return {
        "tree_digest": sha256_bytes(canonical_json(records)),
        "tree_bytes": byte_counter["value"],
        "tree_paths": len(records),
        "entrypoint_sha256": entrypoint_sha256,
        "entrypoint_resolved": str(entrypoint.relative_to(root)),
        "package_wrapper": str(PACKAGE_WRAPPER_RELATIVE),
        "package_wrapper_sha256": package_wrapper_sha256,
    }


def build_software_manifest(root: Path, *, version: str = TESTED_CLI_VERSION) -> dict[str, Any]:
    return {
        **software_manifest_identity(version=version),
        **compute_software_tree_digest(root),
    }


def path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def software_presence(target: Path) -> dict[str, Any]:
    replace_paths_present = [
        str(relative) for relative in SOFTWARE_REPLACE_PATHS if path_present(target / relative)
    ]
    owned_parent_paths_present = [
        str(relative) for relative in SOFTWARE_PARENT_PATHS if path_present(target / relative)
    ]
    if not replace_paths_present and not owned_parent_paths_present:
        state = "absent"
    elif len(replace_paths_present) == len(SOFTWARE_REPLACE_PATHS):
        state = "installed"
    else:
        state = "partial"
    return {
        "software_state": state,
        "partial": state == "partial",
        "replace_paths_present": replace_paths_present,
        "owned_parent_paths_present": owned_parent_paths_present,
    }


def software_status(target: Path) -> dict[str, Any]:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(target),
            "version": None,
            "executable": None,
            "software_state": "absent",
            "partial": False,
            "replace_paths_present": [],
            "owned_parent_paths_present": [],
            "extension_supported": False,
            "extension_installed": None,
        }
    if stat.S_ISLNK(info.st_mode):
        fail("target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a directory")
    if not is_owner_private_directory(info):
        fail("target must be owned by the current user with mode 0700")
    canonical_target = target.resolve()
    executable = cline_executable(canonical_target)
    manifest = software_manifest_path(canonical_target)
    presence = software_presence(canonical_target)
    if presence["software_state"] != "installed":
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(canonical_target),
            "version": None,
            "executable": str(executable),
            **presence,
            "extension_supported": False,
            "extension_installed": None,
        }
    try:
        info = load_json_object(manifest, "software manifest", owner_only=True)
    except ClineSetupError as exc:
        return {
            "ok": True,
            "installed": True,
            "current": False,
            "target": str(canonical_target),
            "version": None,
            "executable": str(executable),
            **presence,
            "validation_error": str(exc),
            "extension_supported": False,
            "extension_installed": None,
        }
    try:
        expected = build_software_manifest(canonical_target)
    except ClineSetupError as exc:
        expected = None
        validation_error = str(exc)
    else:
        validation_error = None
    current = expected is not None and info == expected
    result = {
        "ok": True,
        "installed": True,
        "current": current,
        "target": str(canonical_target),
        "version": info.get("version"),
        "executable": str(executable),
        "package": info.get("package"),
        "package_manager": info.get("package_manager"),
        "install_method": info.get("install_method"),
        **presence,
        "extension_supported": False,
        "extension_installed": None,
    }
    if validation_error is not None:
        result["validation_error"] = validation_error
    return result


def install_stage_environment(stage_root: Path, live_stage: Path) -> dict[str, str]:
    home = stage_root / "home"
    cache = stage_root / "cache"
    tmp = stage_root / "tmp"
    xdg_config = stage_root / "xdg-config"
    xdg_cache = stage_root / "xdg-cache"
    xdg_state = stage_root / "xdg-state"
    global_dir = live_stage / SOFTWARE_DIR_RELATIVE / "install" / "global"
    bin_dir = live_stage / "bin"
    cline_data = stage_root / "cline-data"
    cline_sandbox = stage_root / "cline-sandbox"
    for directory in (
        home,
        cache,
        tmp,
        xdg_config,
        xdg_cache,
        xdg_state,
        global_dir,
        bin_dir,
        cline_data,
        cline_sandbox,
    ):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    env = safe_child_base_environment()
    env.update(
        {
            "BUN_INSTALL_GLOBAL_DIR": str(global_dir),
            "BUN_INSTALL_BIN": str(bin_dir),
            "BUN_INSTALL_CACHE_DIR": str(cache),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
            "CLINE_DATA_DIR": str(cline_data),
            "CLINE_SANDBOX": "true",
            "CLINE_SANDBOX_DATA_DIR": str(cline_sandbox),
        }
    )
    return env


def normalize_stage_executable(live_stage: Path) -> None:
    executable = live_stage / "bin" / COMMAND_NAME
    package_wrapper = live_stage / PACKAGE_WRAPPER_RELATIVE
    require_safe_executable(
        package_wrapper,
        live_stage,
        "Cline package wrapper",
        allow_hardlinks=False,
        owner_only_mode=False,
    )
    try:
        info = executable.lstat()
    except FileNotFoundError:
        fail("bun did not create bin/cline")
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = executable.resolve(strict=True)
        except FileNotFoundError:
            fail("bun created a broken bin/cline symlink")
        if not path_is_relative_to(resolved, live_stage.resolve()):
            fail("bun created a bin/cline symlink outside the staging tree")
    elif not stat.S_ISREG(info.st_mode):
        fail("bun did not create a regular bin/cline executable")
    executable.unlink()
    package_from_bin = Path("..") / PACKAGE_WRAPPER_RELATIVE
    wrapper = (
        "#!/bin/sh\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f'exec "$SCRIPT_DIR"/{shlex.quote(str(package_from_bin))} "$@"\n'
    )
    executable.write_text(wrapper, encoding="utf-8")
    executable.chmod(OWNER_EXECUTABLE_MODE)
    return


def chmod_private_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        if path.is_symlink():
            continue
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            path.chmod(OWNER_DIRECTORY_MODE)
        elif stat.S_ISREG(info.st_mode):
            if stat.S_IMODE(info.st_mode) & stat.S_IXUSR:
                path.chmod(OWNER_EXECUTABLE_MODE)
            else:
                path.chmod(OWNER_FILE_MODE)


def cleanup_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def validate_replace_destination(target: Path, relative: Path) -> None:
    destination = target / relative
    if not destination.exists() and not destination.is_symlink():
        return
    info = destination.lstat()
    if relative == Path("bin") / COMMAND_NAME:
        require_safe_executable(
            destination, target, "existing Cline CLI executable", allow_hardlinks=False
        )
        return
    if stat.S_ISLNK(info.st_mode):
        fail(f"existing software path {relative} must not be a symlink")
    if stat.S_ISDIR(info.st_mode):
        if not is_owner_private_directory(info):
            fail(f"existing software directory {relative} must be private to the current user")
        return
    if stat.S_ISREG(info.st_mode):
        require_regular_file(destination, f"existing software file {relative}", owner_only=True)
        return
    fail(f"existing software path {relative} has an unsafe type")


def validate_software_parent_destination(target: Path, relative: Path) -> None:
    parent = target / relative
    if not path_present(parent):
        return
    info = require_directory(parent, f"existing software parent {relative}")
    if not is_owner_private_directory(info):
        fail(f"existing software parent {relative} must be private to the current user")


def validate_existing_software_surface(target: Path) -> None:
    for relative in SOFTWARE_PARENT_PATHS:
        validate_software_parent_destination(target, relative)
    for relative in SOFTWARE_REPLACE_PATHS:
        if path_present(target / relative):
            validate_replace_destination(target, relative)


def ensure_replace_parent(destination: Path) -> None:
    parent = destination.parent
    try:
        info = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
        parent.chmod(OWNER_DIRECTORY_MODE)
        return
    if stat.S_ISLNK(info.st_mode):
        fail(f"software destination parent {parent} must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail(f"software destination parent {parent} must be a directory")
    if not is_owner_private_directory(info):
        fail(f"software destination parent {parent} must be private to the current user")


def move_replace_path(source: Path, destination: Path) -> None:
    ensure_replace_parent(destination)
    os.replace(source, destination)


def move_old_path(source: Path, saved: Path) -> None:
    saved.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    os.replace(source, saved)


def restore_software_paths(
    target: Path,
    hold: Path,
    live_stage: Path,
    *,
    moved_old: list[Path],
    installed_new: list[Path],
    preexisting_parent_paths: set[Path],
) -> None:
    new_paths = set(installed_new)
    for relative in SOFTWARE_REPLACE_PATHS:
        if relative not in new_paths and not (live_stage / relative).exists():
            new_paths.add(relative)
    for relative in reversed(SOFTWARE_REPLACE_PATHS):
        destination = target / relative
        if relative in new_paths and (destination.exists() or destination.is_symlink()):
            cleanup_path(destination)
    for relative in reversed(moved_old):
        saved = hold / relative
        if not saved.exists() and not saved.is_symlink():
            continue
        move_replace_path(saved, target / relative)
    for relative in sorted(SOFTWARE_PARENT_PATHS, key=lambda item: len(item.parts), reverse=True):
        if relative in preexisting_parent_paths:
            continue
        parent = target / relative
        if not parent.exists() or parent.is_symlink() or not parent.is_dir():
            continue
        try:
            parent.rmdir()
        except OSError:
            continue


def replace_software_state(target: Path, live_stage: Path, hold_parent: Path) -> None:
    for relative in SOFTWARE_REPLACE_PATHS:
        source = live_stage / relative
        if not source.exists() and not source.is_symlink():
            fail(f"staged software path {relative} is missing")
        validate_replace_destination(target, relative)
    hold = hold_parent / "rollback"
    if hold.exists() or hold.is_symlink():
        cleanup_path(hold)
    hold.mkdir(mode=OWNER_DIRECTORY_MODE)
    preexisting_parent_paths = {
        relative for relative in SOFTWARE_PARENT_PATHS if path_present(target / relative)
    }
    moved_old: list[Path] = []
    installed_new: list[Path] = []
    try:
        for relative in SOFTWARE_REPLACE_PATHS:
            destination = target / relative
            if destination.exists() or destination.is_symlink():
                saved = hold / relative
                move_old_path(destination, saved)
                moved_old.append(relative)
        for relative in SOFTWARE_REPLACE_PATHS:
            move_replace_path(live_stage / relative, target / relative)
            installed_new.append(relative)
        status = software_status(target)
        if not status["installed"] or not status["current"]:
            fail("installed Cline CLI did not validate as the tested version")
    except BaseException:
        moved_old = [
            relative
            for relative in SOFTWARE_REPLACE_PATHS
            if (hold / relative).exists() or (hold / relative).is_symlink()
        ]
        restore_software_paths(
            target,
            hold,
            live_stage,
            moved_old=moved_old,
            installed_new=installed_new,
            preexisting_parent_paths=preexisting_parent_paths,
        )
        raise
    finally:
        shutil.rmtree(hold, ignore_errors=True)


def run_bun_install(stage_root: Path, live_stage: Path) -> None:
    env = install_stage_environment(stage_root, live_stage)
    completed = run_bounded_process(
        ["bun", *BUN_INSTALL_ARGV],
        cwd=stage_root,
        env=env,
        label="bun Cline CLI install",
    )
    if completed.returncode != 0:
        fail("bun failed to install the pinned Cline CLI")
    normalize_stage_executable(live_stage)
    chmod_private_tree(live_stage)
    observed = observed_cline_version(live_stage / "bin" / COMMAND_NAME, target=live_stage)
    if observed != TESTED_CLI_VERSION:
        fail(f"bun installed Cline CLI {observed}, expected {TESTED_CLI_VERSION}")


def write_stage_manifest(live_stage: Path) -> None:
    manifest = live_stage / SOFTWARE_MANIFEST_RELATIVE
    manifest.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    manifest.write_bytes(canonical_json(build_software_manifest(live_stage)))
    manifest.chmod(OWNER_FILE_MODE)


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    if operation == "update-cli":
        try:
            info = target.lstat()
        except FileNotFoundError:
            fail("Cline CLI is not installed; use install-cli")
        if stat.S_ISLNK(info.st_mode):
            fail("target must not be a symlink")
        if not stat.S_ISDIR(info.st_mode):
            fail("target must be a directory")
        if not is_owner_private_directory(info):
            fail("target must be owned by the current user with mode 0700")
        canonical_target = target.resolve()
    else:
        canonical_target = ensure_target_directory(target)
    with target_lock(canonical_target):
        status = software_status(canonical_target)
        if status["installed"] and status["current"]:
            return {
                "ok": True,
                "operation": operation,
                "target": str(canonical_target),
                "version": TESTED_CLI_VERSION,
                "package": NPM_PACKAGE,
                "package_manager": "bun",
                "install_method": "bun-global",
                "executable": str(cline_executable(canonical_target)),
                "changed": False,
            }
        if operation == "install-cli":
            if status.get("partial"):
                fail(
                    "partial target-owned Cline CLI software state exists; "
                    "use update-cli or repair/remove the target-owned software paths"
                )
            if status["installed"]:
                fail("Cline CLI is already installed but not current; use update-cli")
            if status.get("owned_parent_paths_present") or status.get("replace_paths_present"):
                fail(
                    "target-owned Cline CLI software paths already exist; "
                    "use update-cli or repair/remove them"
                )
        if operation == "update-cli" and status["software_state"] == "absent":
            fail("Cline CLI is not installed; use install-cli")
        if operation == "update-cli":
            validate_existing_software_surface(canonical_target)
        staging = Path(
            tempfile.mkdtemp(
                dir=canonical_target.parent,
                prefix=f".{canonical_target.name}.nddev-cline-cli-stage.",
            )
        )
        staging.chmod(OWNER_DIRECTORY_MODE)
        try:
            live_stage = staging / "live"
            live_stage.mkdir(mode=OWNER_DIRECTORY_MODE)
            run_bun_install(staging, live_stage)
            chmod_private_tree(live_stage)
            write_stage_manifest(live_stage)
            replace_software_state(canonical_target, live_stage, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "operation": operation,
        "target": str(canonical_target),
        "version": TESTED_CLI_VERSION,
        "package": NPM_PACKAGE,
        "package_manager": "bun",
        "install_method": "bun-global",
        "executable": str(cline_executable(canonical_target)),
        "changed": True,
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
    env = safe_child_base_environment()
    for name in ("TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR"):
        value = os.environ.get(name)
        if value:
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


def validate_launch_args(args: list[str]) -> None:
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in BLOCKED_LAUNCH_FLAGS or arg.startswith("-k=") or (
            arg.startswith("-k") and arg != "-k" and not arg.startswith("--")
        ):
            fail(f"launch argument {flag!r} is managed by nddev-cline-app")


def launch_cline(target: Path, args: list[str]) -> int:
    validate_launch_args(args)
    canonical_target = require_explicit_absolute_target(str(target))
    with target_lock(canonical_target):
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
        executable = cline_executable(canonical_target)
        child_env = isolated_child_environment(canonical_target, state["command_permissions"])
    completed = subprocess.run(
        [str(executable), *child_args],
        cwd=os.getcwd(),
        env=child_env,
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
