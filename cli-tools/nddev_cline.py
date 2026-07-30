#!/usr/bin/env python3
"""Transactional setup manager for an explicit Cline target."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import platform
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

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is rejected for runtime lifecycle.
    fcntl = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
PROFILE_ROOT = ROOT / "profiles"
BUILDER_ROOT = ROOT / "plugins" / "nddev-builder"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-cline-app"
COMMAND_NAME = "cline"
STAMP_NAME = "NDDEV-CLINE-SETUP.json"
BACKUP_POOL_NAME = "NDDEV-CLINE-BACKUPS.json"
BACKUP_NAME = "NDDEV-CLINE-BACKUP.json"
LOCK_DIRECTORY_NAME = ".nddev-cline-lock"
LOCK_FILE_NAME = "lock"
BOOTSTRAP_LOCK_POOL_NAME = ".nddev-cline-lifecycle-locks"
PRODUCT_LOCK_NAME = "global.lock"
BOOTSTRAP_LOCK_SUFFIX = ".lock"
BASELINE_REF = ROOT / "references" / "cline-baseline.json"
INSTALL_LOCK_ROOT = ROOT / "software" / "cline-cli"
INSTALL_PACKAGE_JSON = INSTALL_LOCK_ROOT / "package.json"
INSTALL_PACKAGE_LOCK = INSTALL_LOCK_ROOT / "package-lock.json"
TESTED_CLI_VERSION = "3.0.47"
TESTED_EXTENSION_VERSION = "4.0.12"
NPM_PACKAGE = "cline"
NPM_PACKAGE_SPEC = f"{NPM_PACKAGE}@{TESTED_CLI_VERSION}"
NPM_REGISTRY = "https://registry.npmjs.org/"
EXPECTED_CLINE_OPTIONAL_PACKAGES = {
    "@cline/cli-darwin-arm64",
    "@cline/cli-darwin-x64",
    "@cline/cli-linux-arm64",
    "@cline/cli-linux-x64",
    "@cline/cli-windows-arm64",
    "@cline/cli-windows-x64",
}
SUPPORTED_NATIVE_OPTIONAL_PACKAGES = {
    "@cline/cli-darwin-arm64": {"os": ["darwin"], "cpu": ["arm64"], "bin": "bin/cline"},
    "@cline/cli-darwin-x64": {"os": ["darwin"], "cpu": ["x64"], "bin": "bin/cline"},
    "@cline/cli-linux-arm64": {"os": ["linux"], "cpu": ["arm64"], "bin": "bin/cline"},
    "@cline/cli-linux-x64": {"os": ["linux"], "cpu": ["x64"], "bin": "bin/cline"},
}
MIN_NODE_MAJOR = 20
RECOMMENDED_NODE_MAJOR = 22
DEFAULT_SETUP_ID = "nddev-builder"
DEFAULT_PROFILE_ID = "full-auto"
LEGACY_BUILD_VERSIONS = {"0.1.0"}
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700
OWNER_EXECUTABLE_MODE = 0o700
PROTECTED_DIRECTORY_MODE = 0o500
PROTECTED_EXECUTABLE_MODE = 0o500
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 8 * 1024 * 1024
SOFTWARE_TREE_MAX_BYTES = 512 * 1024 * 1024
# Keep this limit synchronized with build/manifest.json software_lifecycle.bounds.
SOFTWARE_TREE_MAX_PATHS = 50000
PROCESS_OUTPUT_MAX_BYTES = 64 * 1024
PROCESS_TIMEOUT_SECONDS = 120
VERSION_PROBE_TIMEOUT_SECONDS = 60
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
VERSION_PATTERN = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
BOOTSTRAP_LOCK_KEYS = {
    "schema_version",
    "product_name",
    "scope",
    "canonical_target",
    "target_key",
}
SOFTWARE_DIR_RELATIVE = Path("software") / "cline-cli"
SOFTWARE_MANIFEST_RELATIVE = Path("software") / "cline-cli.json"
MANAGER_CONTROL_RELATIVE = Path(".nddev-cline-control")
CLEANUP_PARENT_RELATIVE = MANAGER_CONTROL_RELATIVE / "cleanup"
CLEANUP_TOMBSTONES_RELATIVE = CLEANUP_PARENT_RELATIVE / "tombstones"
CLEANUP_PENDING_NAME = "pending.json"
CLEANUP_INTENT_PREFIX = "intent-"
CLEANUP_SCHEMA_VERSION = 2
CLEANUP_MAX_ENTRIES = 20000
CLEANUP_JOURNAL_MAX_BYTES = 2 * 1024 * 1024
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
INSTALL_PROJECT_RELATIVE = SOFTWARE_DIR_RELATIVE / "install" / "project"
PACKAGE_WRAPPER_RELATIVE = (
    INSTALL_PROJECT_RELATIVE
    / "node_modules"
    / "cline"
    / "bin"
    / "cline"
)
CLINE_HOME_RELATIVE = Path("home") / ".cline"
CLINE_CONFIG_RELATIVE = CLINE_HOME_RELATIVE / "data" / "settings"
CLINE_HOOKS_RELATIVE = CLINE_HOME_RELATIVE / "hooks"
CLINE_SANDBOX_RELATIVE = Path("sandbox")
CLINE_GLOBAL_SETTINGS_RELATIVE = CLINE_CONFIG_RELATIVE / "global-settings.json"
CLINE_MCP_SETTINGS_RELATIVE = CLINE_CONFIG_RELATIVE / "cline_mcp_settings.json"
CLINE_RULES_RELATIVE = CLINE_HOME_RELATIVE / "rules" / "nddev-managed.md"
MANAGED_SETTINGS_KEYS: tuple[str, ...] = ()
LEGACY_MANAGED_SETTINGS_KEYS = (
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


def discover_builder_source_files() -> tuple[tuple[Path, Path], ...]:
    if not BUILDER_ROOT.is_dir() or BUILDER_ROOT.is_symlink():
        raise RuntimeError("builder source root is missing")
    result: list[tuple[Path, Path]] = []
    for path in sorted(BUILDER_ROOT.rglob("*"), key=lambda item: str(item.relative_to(BUILDER_ROOT))):
        relative = path.relative_to(BUILDER_ROOT)
        if path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"unsafe builder source path: {relative}")
        result.append((relative, CLINE_HOME_RELATIVE / relative))
    return tuple(result)


BUILDER_SOURCE_FILES = discover_builder_source_files()
MANAGED_PATHS = (
    CLINE_GLOBAL_SETTINGS_RELATIVE,
    CLINE_MCP_SETTINGS_RELATIVE,
    CLINE_RULES_RELATIVE,
    *(target for _, target in BUILDER_SOURCE_FILES),
    Path(STAMP_NAME),
)
LEGACY_MANAGED_PATHS = (
    Path("data") / "settings" / "global-settings.json",
    Path("data") / "settings" / "cline_mcp_settings.json",
    Path("rules") / "nddev-managed.md",
    Path("data") / "settings" / "skills" / "nddev-builder" / "SKILL.md",
    Path("skills") / "nddev-builder" / "SKILL.md",
    Path("agents") / "nddev-builder.md",
    Path("plugins") / "nddev-builder" / "package.json",
    Path("plugins") / "nddev-builder" / "index.js",
    Path(STAMP_NAME),
)
ALL_MANAGED_PATHS = tuple(dict.fromkeys((*MANAGED_PATHS, *LEGACY_MANAGED_PATHS)))
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "profile_id",
    "canonical_target",
    "managed_files",
    "builder_projection",
    "launch_args",
    "command_permissions",
}
LEGACY_STAMP_KEYS = {
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
    "files",
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
TRUSTED_TOOL_PATHS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
)
DETERMINISTIC_PATH = os.pathsep.join(TRUSTED_TOOL_PATHS)
BLOCKED_LAUNCH_FLAGS = {
    "--auto-approve",
    "--config",
    "--data-dir",
    "--hooks-dir",
    "--key",
    "--plan",
    "--provider",
    "--yolo",
    "--zen",
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


def is_owner_protected_directory(info: os.stat_result) -> bool:
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != PROTECTED_DIRECTORY_MODE:
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


def legacy_managed_settings_view(settings: dict[str, Any]) -> dict[str, Any]:
    return {key: settings[key] for key in LEGACY_MANAGED_SETTINGS_KEYS if key in settings}


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
    if relative == CLINE_GLOBAL_SETTINGS_RELATIVE:
        settings = parse_json_object(content, str(relative))
        return sha256_bytes(canonical_json(managed_settings_view(settings)))
    return sha256_bytes(content)


def legacy_managed_digest(relative: Path, content: bytes) -> str:
    if relative == Path("data") / "settings" / "global-settings.json":
        settings = parse_json_object(content, str(relative))
        return sha256_bytes(canonical_json(legacy_managed_settings_view(settings)))
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
    if settings:
        fail(f"{label} must not declare unverified global settings keys")


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
        },
        f"setup {setup_id} metadata",
    )
    if metadata["schema_version"] != 1:
        fail(f"setup {setup_id} metadata has unsupported schema")
    if metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity mismatch")
    if metadata["managed_files"] != [
        str(CLINE_GLOBAL_SETTINGS_RELATIVE),
        str(CLINE_MCP_SETTINGS_RELATIVE),
        str(CLINE_RULES_RELATIVE),
    ]:
        fail(f"setup {setup_id} managed file declaration is invalid")
    if (
        metadata["builder_projection"] != "native-skills-agents-plugin-user-files"
        or metadata["builder_default_on"] is not True
    ):
        fail(f"setup {setup_id} must enable native builder projection")
    if metadata["id"] != DEFAULT_SETUP_ID:
        fail(f"setup {setup_id} is not the supported content setup")


def validate_profile_metadata(metadata: dict[str, Any], profile_id: str) -> None:
    require_exact_keys(
        metadata,
        {
            "schema_version",
            "id",
            "description",
            "default",
            "sandbox",
            "launch_args",
            "command_permissions",
        },
        f"profile {profile_id} metadata",
    )
    if metadata["schema_version"] != 1:
        fail(f"profile {profile_id} metadata has unsupported schema")
    if metadata["id"] != profile_id:
        fail(f"profile {profile_id} metadata identity mismatch")
    if profile_id not in {"safe", "full-auto"}:
        fail(f"unsupported profile: {profile_id}")
    if not isinstance(metadata["default"], bool):
        fail(f"profile {profile_id} default must be boolean")
    if not isinstance(metadata["sandbox"], bool):
        fail(f"profile {profile_id} sandbox must be boolean")
    if not isinstance(metadata["launch_args"], list) or not all(
        isinstance(item, str) for item in metadata["launch_args"]
    ):
        fail(f"profile {profile_id} launch_args must be a string array")
    validate_command_permissions(metadata["command_permissions"], f"profile {profile_id}")
    if profile_id == "full-auto":
        if metadata["default"] is not True or metadata["sandbox"] is not False:
            fail("full-auto must be the default non-sandbox profile")
        if metadata["launch_args"] != ["--auto-approve", "true"]:
            fail("full-auto launch args must enable auto-approve only")
        if metadata["command_permissions"] != {
            "allow": ["*"],
            "deny": [],
            "allowRedirects": True,
        }:
            fail("full-auto command permissions must allow everything")
    if profile_id == "safe":
        if metadata["default"] is not False or metadata["sandbox"] is not True:
            fail("safe must be the non-default sandbox profile")
        if metadata["launch_args"] != ["--plan", "--auto-approve", "false"]:
            fail("safe launch args must be plan mode with auto-approve disabled")
        if metadata["command_permissions"] != {
            "allow": [],
            "deny": ["*"],
            "allowRedirects": False,
        }:
            fail("safe command permissions must deny command execution")


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
    profile_id: str,
    desired: dict[Path, bytes],
    launch_args: list[str],
    command_permissions: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "profile_id": profile_id,
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
    setup_id: str,
    profile_id: str,
    *,
    existing_settings: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    validate_setup_id(setup_id)
    validate_setup_id(profile_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")
    profile_root = PROFILE_ROOT / profile_id
    if not profile_root.is_dir() or profile_root.is_symlink():
        fail(f"unknown profile: {profile_id}")
    metadata = load_json_object(setup_root / "setup.json", f"setup {setup_id} metadata")
    validate_setup_metadata(metadata, setup_id)
    profile = load_json_object(profile_root / "profile.json", f"profile {profile_id} metadata")
    validate_profile_metadata(profile, profile_id)
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
        CLINE_GLOBAL_SETTINGS_RELATIVE: canonical_json(merged_settings),
        CLINE_MCP_SETTINGS_RELATIVE: canonical_json(mcp_settings),
        CLINE_RULES_RELATIVE: rules_md,
    }
    desired.update(render_builder_files())
    desired[Path(STAMP_NAME)] = canonical_json(
        build_stamp(
            setup_id,
            profile_id,
            desired,
            profile["launch_args"],
            profile["command_permissions"],
        )
    )
    return metadata, desired


def list_setups() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(CATALOG_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        if not (path / "setup.json").is_file():
            continue
        metadata = load_json_object(path / "setup.json", f"setup {path.name} metadata")
        validate_setup_metadata(metadata, path.name)
        result.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "builder_default_on": metadata["builder_default_on"],
            }
        )
    return result


def list_profiles() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(PROFILE_ROOT.iterdir()):
        if not path.is_dir() or path.is_symlink():
            continue
        if not (path / "profile.json").is_file():
            continue
        metadata = load_json_object(path / "profile.json", f"profile {path.name} metadata")
        validate_profile_metadata(metadata, path.name)
        result.append(
            {
                "id": metadata["id"],
                "description": metadata["description"],
                "default": metadata["default"],
                "sandbox": metadata["sandbox"],
                "launch_args": metadata["launch_args"],
            }
        )
    return result


def backup_pool(target: Path) -> Path:
    return target / ".nddev-cline-backups"


def legacy_backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-cline-backups"


def backup_pool_marker(pool: Path) -> Path:
    return pool / BACKUP_POOL_NAME


def lock_path(target: Path) -> Path:
    return lock_directory_path(target) / LOCK_FILE_NAME


def lock_directory_path(target: Path) -> Path:
    return target / LOCK_DIRECTORY_NAME


def bootstrap_lock_key(canonical_target: Path) -> str:
    content = f"{PRODUCT_NAME}\0{canonical_target}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def fixed_system_temp_root() -> Path:
    root = Path("/private/tmp") if sys.platform.startswith("darwin") else Path("/tmp")
    resolved = root.resolve()
    info = require_directory(resolved, "bootstrap system temp root")
    if not stat.S_IMODE(info.st_mode) & stat.S_ISVTX:
        fail("bootstrap system temp root must be sticky")
    return resolved


def bootstrap_lock_pool(_canonical_target: Path) -> Path:
    if hasattr(os, "geteuid"):
        uid: int | str = os.geteuid()
    elif hasattr(os, "getuid"):
        uid = os.getuid()
    else:
        uid = "unknown"
    return fixed_system_temp_root() / f".{PRODUCT_NAME}-{uid}-lifecycle-locks"


def product_lock_path_without_create() -> Path:
    return bootstrap_lock_pool(Path("/")) / PRODUCT_LOCK_NAME


def bootstrap_lock_path(canonical_target: Path) -> Path:
    return bootstrap_lock_pool(canonical_target) / f"{bootstrap_lock_key(canonical_target)}{BOOTSTRAP_LOCK_SUFFIX}"


def sync_directory(path: Path, label: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        fail(f"cannot open {label} for sync: {exc}")
    try:
        os.fsync(descriptor)
    except OSError as exc:
        fail(f"cannot sync {label}: {exc}")
    finally:
        os.close(descriptor)


def ensure_bootstrap_lock_pool(canonical_target: Path | None = None) -> Path:
    pool = bootstrap_lock_pool(canonical_target)
    created = False
    try:
        info = pool.lstat()
    except FileNotFoundError:
        try:
            pool.mkdir(mode=OWNER_DIRECTORY_MODE)
        except FileExistsError:
            info = pool.lstat()
        else:
            created = True
            try:
                pool.chmod(OWNER_DIRECTORY_MODE)
                sync_directory(pool.parent, "bootstrap lifecycle lock parent")
                info = pool.lstat()
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    pool.rmdir()
                    sync_directory(pool.parent, "bootstrap lifecycle lock parent")
                raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("bootstrap lifecycle lock pool must be a real directory")
    if not is_owner_private_directory(info):
        fail("bootstrap lifecycle lock pool must be owned by the current user with mode 0700")
    if created:
        sync_directory(pool, "bootstrap lifecycle lock pool")
    return pool


def canonical_target_for_bootstrap_lock(target: Path) -> Path:
    if not target.is_absolute():
        fail("--target must be an absolute path")
    if target.name in {"", ".", ".."}:
        fail("--target must include a literal target directory name")
    return target


def require_lock_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    return info


def lock_open_flags(*, write: bool = True) -> int:
    flags = os.O_RDWR if write else os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def write_all(descriptor: int, content: bytes, *, label: str) -> None:
    view = memoryview(content)
    while view:
        try:
            written = os.write(descriptor, view)
        except OSError as exc:
            fail(f"cannot write {label}: {exc}")
        if written <= 0:
            fail(f"cannot write {label}: short write")
        view = view[written:]


def publication_alias_prefix(path: Path) -> str:
    return f".{path.name}.publish-"


def publish_anchor_no_replace(path: Path, content: bytes, label: str) -> None:
    parent = path.parent
    ensure_bootstrap_lock_pool(None)
    temporary = parent / f"{publication_alias_prefix(path)}{os.getpid()}-{time.time_ns()}"
    descriptor: int | None = None
    linked = False
    try:
        try:
            descriptor = os.open(
                temporary,
                lock_open_flags(write=True) | os.O_CREAT | os.O_EXCL,
                OWNER_FILE_MODE,
            )
        except OSError as exc:
            fail(f"cannot create temporary {label}: {exc}")
        try:
            write_all(descriptor, content, label=label)
            os.fchmod(descriptor, OWNER_FILE_MODE)
            os.fsync(descriptor)
        except OSError as exc:
            fail(f"cannot prepare {label}: {exc}")
        finally:
            os.close(descriptor)
            descriptor = None
        try:
            os.link(temporary, path)
            linked = True
        except FileExistsError:
            return
        except OSError as exc:
            fail(f"cannot publish {label}: {exc}")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            fail(f"cannot remove temporary {label} alias after publication: {exc}")
        sync_directory(parent, f"{label} parent")
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if not linked:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def publish_regular_file_no_replace(path: Path, content: bytes, label: str) -> None:
    if len(content) > CLEANUP_JOURNAL_MAX_BYTES:
        fail(f"{label} exceeds the serialized metadata size limit")
    parent = path.parent
    parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    parent.chmod(OWNER_DIRECTORY_MODE)
    temporary = parent / f".{path.name}.publish-{os.getpid()}-{time.time_ns()}"
    descriptor: int | None = None
    linked = False
    try:
        try:
            descriptor = os.open(
                temporary,
                lock_open_flags(write=True) | os.O_CREAT | os.O_EXCL,
                OWNER_FILE_MODE,
            )
        except OSError as exc:
            fail(f"cannot create temporary {label}: {exc}")
        try:
            write_all(descriptor, content, label=label)
            os.fchmod(descriptor, OWNER_FILE_MODE)
            os.fsync(descriptor)
        except OSError as exc:
            fail(f"cannot prepare {label}: {exc}")
        finally:
            os.close(descriptor)
            descriptor = None
        try:
            os.link(temporary, path)
            linked = True
        except FileExistsError:
            fail(f"{label} already exists")
        except OSError as exc:
            fail(f"cannot publish {label}: {exc}")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            fail(f"cannot remove temporary {label} alias after publication: {exc}")
        sync_directory(parent, f"{label} parent")
    finally:
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if not linked:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()


def write_regular_file_atomic(path: Path, content: bytes, mode: int, label: str) -> None:
    parent = path.parent
    parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    parent.chmod(OWNER_DIRECTORY_MODE)
    temporary = parent / f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    try:
        descriptor = os.open(
            temporary,
            lock_open_flags(write=True) | os.O_CREAT | os.O_EXCL,
            mode,
        )
    except OSError as exc:
        fail(f"cannot create temporary {label}: {exc}")
    try:
        try:
            write_all(descriptor, content, label=label)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except OSError as exc:
            fail(f"cannot prepare {label}: {exc}")
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        os.chmod(path, mode)
        sync_directory(parent, f"{label} parent")
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def recover_anchor_publication_alias(path: Path, descriptor: int, label: str) -> None:
    opened = os.fstat(descriptor)
    parent = path.parent
    aliases: list[Path] = []
    try:
        entries = list(parent.iterdir())
    except OSError as exc:
        fail(f"cannot inspect {label} publication aliases: {exc}")
    for entry in entries:
        if entry == path or not entry.name.startswith(publication_alias_prefix(path)):
            continue
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and identity_of(info) == identity_of(opened):
            aliases.append(entry)
        elif entry.name.startswith(publication_alias_prefix(path)):
            fail(f"{label} has an unknown publication alias")
    if len(aliases) != 1:
        fail(f"{label} has an incomplete publication alias")
    try:
        aliases[0].unlink()
    except OSError as exc:
        fail(f"cannot recover {label} publication alias: {exc}")
    sync_directory(parent, f"{label} parent")


def validate_anchor_descriptor(
    descriptor: int,
    path: Path,
    label: str,
    binding: dict[str, Any],
    *,
    recover_alias: bool,
) -> None:
    opened = os.fstat(descriptor)
    current = require_lock_file_allowing_recoverable_alias(path, label, recover_alias=recover_alias)
    if identity_of(current) != identity_of(opened):
        fail_concurrent(f"{label} changed while it was being opened")
    if opened.st_nlink != 1:
        if not recover_alias:
            fail(f"{label} has incomplete publication state")
        recover_anchor_publication_alias(path, descriptor, label)
        opened = os.fstat(descriptor)
        current = require_lock_file_allowing_recoverable_alias(path, label, recover_alias=False)
        if identity_of(current) != identity_of(opened):
            fail_concurrent(f"{label} changed during publication alias recovery")
    if not stat.S_ISREG(opened.st_mode):
        fail(f"{label} must be a regular file")
    if not is_owner_only_file(opened):
        fail(f"{label} must be owned by the current user with mode 0600")
    content = read_lock_file_descriptor(descriptor, label=label)
    parsed = parse_json_object(content, label)
    require_exact_keys(parsed, BOOTSTRAP_LOCK_KEYS, label)
    if parsed != binding:
        fail(f"{label} is bound to a different lifecycle scope")


def require_lock_file_allowing_recoverable_alias(
    path: Path,
    label: str,
    *,
    recover_alias: bool,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1 and not recover_alias:
        fail(f"{label} must not have hard-link aliases")
    if not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    return info


def open_anchor_file(
    path: Path,
    label: str,
    binding: dict[str, Any],
    *,
    create: bool,
    exclusive: bool,
    recover_alias: bool,
) -> int:
    content = canonical_json(binding)
    try:
        descriptor = os.open(path, lock_open_flags(write=True))
    except FileNotFoundError:
        if not create:
            raise
        publish_anchor_no_replace(path, content, label)
        try:
            descriptor = os.open(path, lock_open_flags(write=True))
        except OSError as exc:
            fail(f"cannot open published {label}: {exc}")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            fail(f"{label} must not be a symlink")
        fail(f"cannot open {label} {path}: {exc}")
    acquired = False
    try:
        acquire_file_lock(descriptor, path, exclusive=exclusive)
        acquired = True
        validate_anchor_descriptor(
            descriptor,
            path,
            label,
            binding,
            recover_alias=recover_alias,
        )
    except BaseException:
        if acquired:
            release_file_lock(descriptor)
        os.close(descriptor)
        raise
    return descriptor


def open_persistent_lock_file(path: Path, label: str, *, create: bool) -> int:
    flags = lock_open_flags(write=True)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if not create:
            raise
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
        except FileExistsError:
            descriptor = os.open(path, flags)
        except OSError as exc:
            fail(f"cannot create {label} {path}: {exc}")
        os.fchmod(descriptor, OWNER_FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            fail(f"{label} must not be a symlink")
        fail(f"cannot open {label} {path}: {exc}")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            fail(f"{label} must be a regular file")
        if not is_owner_only_file(opened):
            fail(f"{label} must be owned by the current user with mode 0600")
        current = require_lock_file(path, label)
        if identity_of(current) != identity_of(opened):
            fail_concurrent(f"{label} changed while it was being opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_lock_file_descriptor(descriptor: int, *, label: str) -> bytes:
    try:
        size = os.lseek(descriptor, 0, os.SEEK_END)
        if size > METADATA_MAX_BYTES:
            fail(f"{label} exceeds the metadata size limit")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return os.read(descriptor, size)
    except OSError as exc:
        fail(f"cannot read {label}: {exc}")


def open_lock_file(target: Path, *, create: bool) -> int:
    path = lock_path(target)
    return open_persistent_lock_file(path, "target lock file", create=create)


def acquire_file_lock(descriptor: int, path: Path, *, exclusive: bool = True) -> None:
    if fcntl is None:
        fail("target lifecycle locks require POSIX fcntl.flock")
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
    except BlockingIOError:
        fail(f"target is locked: {path}")
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            fail(f"target is locked: {path}")
        fail(f"cannot acquire target lock {path}: {exc}")


def release_file_lock(descriptor: int) -> None:
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def recover_protected_lock_directory_if_unlocked(target: Path, info: os.stat_result) -> None:
    lock_directory = lock_directory_path(target)
    if not is_owner_protected_directory(info):
        return
    if not path_present(lock_path(target)):
        lock_directory.chmod(OWNER_DIRECTORY_MODE)
        return
    try:
        descriptor = open_lock_file(target, create=False)
    except FileNotFoundError:
        lock_directory.chmod(OWNER_DIRECTORY_MODE)
        return
    try:
        acquire_file_lock(descriptor, lock_path(target))
        lock_directory.chmod(OWNER_DIRECTORY_MODE)
    finally:
        release_file_lock(descriptor)
        os.close(descriptor)


def ensure_lock_directory(target: Path) -> Path:
    require_private_directory(target, "target lock parent")
    lock_directory = lock_directory_path(target)
    try:
        info = lock_directory.lstat()
    except FileNotFoundError:
        lock_directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        lock_directory.chmod(OWNER_DIRECTORY_MODE)
        return lock_directory
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("target lock directory must be a real directory")
    if is_owner_private_directory(info):
        return lock_directory
    recover_protected_lock_directory_if_unlocked(target, info)
    info = require_directory(lock_directory, "target lock directory")
    if is_owner_private_directory(info):
        return lock_directory
    fail("target lock directory must be owned by the current user with mode 0700")


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    path = lock_path(target)
    lock_directory = ensure_lock_directory(target)
    descriptor = open_lock_file(target, create=True)
    acquired = False
    protected = False
    try:
        acquire_file_lock(descriptor, path)
        acquired = True
        lock_directory.chmod(PROTECTED_DIRECTORY_MODE)
        protected = True
        yield
    finally:
        if protected:
            with contextlib.suppress(FileNotFoundError, OSError):
                current = lock_directory.lstat()
                if stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode):
                    lock_directory.chmod(OWNER_DIRECTORY_MODE)
        if acquired:
            release_file_lock(descriptor)
        os.close(descriptor)


def bootstrap_lock_binding(canonical_target: Path) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "product_name": PRODUCT_NAME,
        "scope": "canonical-target",
        "canonical_target": str(canonical_target),
        "target_key": bootstrap_lock_key(canonical_target),
    }


def product_lock_binding() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "product_name": PRODUCT_NAME,
        "scope": "product",
        "canonical_target": None,
        "target_key": None,
    }


def validate_bootstrap_lock_binding(
    descriptor: int,
    path: Path,
    canonical_target: Path,
) -> None:
    validate_anchor_descriptor(
        descriptor,
        path,
        "bootstrap lifecycle lock file",
        bootstrap_lock_binding(canonical_target),
        recover_alias=False,
    )


def verify_locked_file_identity(descriptor: int, path: Path, label: str) -> None:
    opened = os.fstat(descriptor)
    current = require_lock_file(path, label)
    if identity_of(current) != identity_of(opened):
        fail_concurrent(f"{label} changed while locked")
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        fail(f"{label} must be a regular file")
    if not is_owner_only_file(opened):
        fail(f"{label} must be owned by the current user with mode 0600")


@contextlib.contextmanager
def bootstrap_lifecycle_lock(target: Path) -> Iterator[Path]:
    with coordinated_target(target, mutation=True, create_target_anchor=True) as canonical_target:
        yield canonical_target


@contextlib.contextmanager
def product_lifecycle_lock(*, exclusive: bool, create: bool) -> Iterator[int | None]:
    try:
        descriptor = open_product_lifecycle_fd(exclusive=exclusive, create=create)
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            release_file_lock(descriptor)
            os.close(descriptor)


def open_product_lifecycle_fd(*, exclusive: bool, create: bool) -> int:
    return open_anchor_file(
        product_lock_path_without_create(),
        "product lifecycle lock file",
        product_lock_binding(),
        create=create,
        exclusive=exclusive,
        recover_alias=create and exclusive,
    )


@contextlib.contextmanager
def canonical_target_anchor_lock(
    canonical_target: Path,
    *,
    exclusive: bool,
    create: bool,
) -> Iterator[int | None]:
    try:
        descriptor = open_canonical_target_anchor_fd(
            canonical_target,
            exclusive=exclusive,
            create=create,
        )
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    try:
        yield descriptor
    finally:
        if descriptor is not None:
            release_file_lock(descriptor)
            os.close(descriptor)


def open_canonical_target_anchor_fd(
    canonical_target: Path,
    *,
    exclusive: bool,
    create: bool,
) -> int:
    return open_anchor_file(
        bootstrap_lock_path(canonical_target),
        "canonical target lifecycle lock file",
        bootstrap_lock_binding(canonical_target),
        create=create,
        exclusive=exclusive,
        recover_alias=create and exclusive,
    )


def product_anchor_exists() -> bool:
    path = product_lock_path_without_create()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("product lifecycle lock file is malformed")
    return True


def target_anchor_exists(canonical_target: Path) -> bool:
    path = bootstrap_lock_path(canonical_target)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("canonical target lifecycle lock file is malformed")
    return True


def reject_orphan_target_anchor(canonical_target: Path) -> None:
    pool = bootstrap_lock_pool(canonical_target)
    product_path = product_lock_path_without_create()
    target_path = bootstrap_lock_path(canonical_target)
    if not path_present(pool):
        return
    if not path_present(product_path) and path_present(target_path):
        fail("canonical target lifecycle lock exists without product lifecycle lock; explicit repair is required")


def canonical_target_under_coordination(target: Path, *, allow_missing: bool) -> Path:
    if not target.is_absolute():
        fail("--target must be an absolute path")
    if target.name in {"", ".", ".."}:
        fail("--target must include a literal target directory name")
    try:
        info = target.lstat()
    except FileNotFoundError:
        if not allow_missing:
            fail("target is missing")
        parent = target.parent
        require_safe_target_parent_for_creation(parent)
        return parent.resolve() / target.name
    if stat.S_ISLNK(info.st_mode):
        fail("--target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("--target must be a directory")
    return target.resolve()


@contextlib.contextmanager
def coordinated_target(
    target: Path,
    *,
    mutation: bool,
    create_target_anchor: bool,
    allow_missing: bool = True,
) -> Iterator[Path]:
    if mutation:
        product_fd = open_product_lifecycle_fd(exclusive=True, create=True)
        target_fd: int | None = None
        try:
            canonical_target = canonical_target_under_coordination(
                target, allow_missing=allow_missing
            )
            target_fd = open_canonical_target_anchor_fd(
                canonical_target,
                exclusive=True,
                create=create_target_anchor,
            )
        finally:
            release_file_lock(product_fd)
            os.close(product_fd)
        try:
            yield canonical_target
        finally:
            if target_fd is not None:
                release_file_lock(target_fd)
                os.close(target_fd)
        return

    if not product_anchor_exists():
        canonical_target = canonical_target_under_coordination(target, allow_missing=True)
        reject_orphan_target_anchor(canonical_target)
        yield canonical_target
        if product_anchor_exists():
            with coordinated_target(
                target,
                mutation=False,
                create_target_anchor=False,
                allow_missing=allow_missing,
            ) as retried:
                if retried != canonical_target:
                    fail_concurrent("target canonical path changed during read coordination retry")
        return

    product_fd = open_product_lifecycle_fd(exclusive=False, create=False)
    target_fd: int | None = None
    try:
        canonical_target = canonical_target_under_coordination(
            target, allow_missing=allow_missing
        )
        if target_anchor_exists(canonical_target):
            target_fd = open_canonical_target_anchor_fd(
                canonical_target,
                exclusive=False,
                create=False,
            )
            release_file_lock(product_fd)
            os.close(product_fd)
            product_fd = -1
            try:
                yield canonical_target
            finally:
                release_file_lock(target_fd)
                os.close(target_fd)
        else:
            yield canonical_target
    finally:
        if product_fd >= 0:
            release_file_lock(product_fd)
            os.close(product_fd)


@contextlib.contextmanager
def locked_new_or_existing_target(target: Path) -> Iterator[Path]:
    with locked_new_or_existing_target_with_creation(target) as (canonical_target, _created, _parent):
        yield canonical_target


@contextlib.contextmanager
def locked_new_or_existing_target_with_creation(
    target: Path,
) -> Iterator[tuple[Path, bool, dict[str, Any] | None]]:
    with coordinated_target(target, mutation=True, create_target_anchor=True) as locked_target:
        created_target = not path_present(locked_target)
        parent_metadata = directory_metadata(locked_target.parent) if created_target else None
        canonical_target = ensure_target_directory(locked_target)
        if canonical_target != locked_target:
            fail_concurrent("target canonical path changed during lifecycle lock acquisition")
        with target_lock(canonical_target):
            yield canonical_target, created_target, parent_metadata


@contextlib.contextmanager
def locked_existing_target(target: Path) -> Iterator[Path]:
    with coordinated_target(
        target,
        mutation=True,
        create_target_anchor=True,
        allow_missing=False,
    ) as canonical_target:
        with target_lock(canonical_target):
            yield canonical_target


@contextlib.contextmanager
def locked_inspection_target(target: Path) -> Iterator[Path]:
    with coordinated_target(target, mutation=False, create_target_anchor=False) as canonical_target:
        yield canonical_target


def require_absolute_target_argument(raw_target: str | None) -> Path:
    if not raw_target:
        fail("an explicit --target absolute path is required")
    target = Path(raw_target)
    if not target.is_absolute():
        fail("--target must be an absolute path")
    return target


def require_explicit_absolute_target(raw_target: str | None) -> Path:
    target = require_absolute_target_argument(raw_target)
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
        require_safe_target_parent_for_creation(target.parent)
        target.mkdir(mode=OWNER_DIRECTORY_MODE)
        target.chmod(OWNER_DIRECTORY_MODE)
        created = target.resolve()
        require_private_directory(created, "target")
        return created
    if stat.S_ISLNK(info.st_mode):
        fail("target must not be a symlink")
    if not stat.S_ISDIR(info.st_mode):
        fail("target must be a directory")
    if not is_owner_private_directory(info):
        fail("target must be owned by the current user with mode 0700")
    return target.resolve()


def require_safe_target_parent_for_creation(parent: Path) -> None:
    info = require_directory(parent, "target parent")
    if is_owner_private_directory(info):
        return
    mode = stat.S_IMODE(info.st_mode)
    if mode & stat.S_ISVTX:
        return
    fail("target parent must be private to the current user or sticky")


def ensure_private_directory_under_target(target: Path, relative: Path, label: str) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        fail(f"unsafe {label}: {relative}")
    current = target
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                fail(f"{label} {current.relative_to(target)} must be a real directory")
            if not is_owner_private_directory(info):
                fail(
                    f"{label} {current.relative_to(target)} "
                    "must be owned by the current user with mode 0700"
                )
        else:
            current.mkdir(mode=OWNER_DIRECTORY_MODE)
            current.chmod(OWNER_DIRECTORY_MODE)
    return current


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
        for relative in ALL_MANAGED_PATHS
    )


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp = target / STAMP_NAME
    if not stamp.exists() and not stamp.is_symlink():
        return None
    value = load_json_object(stamp, "setup stamp", owner_only=True)
    actual_keys = set(value)
    if actual_keys == STAMP_KEYS:
        if (
            value["schema_version"] != 1
            or value["product_name"] != PRODUCT_NAME
        ):
            fail("setup stamp schema or product identity is not compatible with this manager")
        if not isinstance(value["build_version"], str):
            fail("setup stamp build_version must be a string")
        validate_setup_id(value["profile_id"])
        value["legacy"] = False
        value["needs_update"] = value["build_version"] != VERSION
    elif actual_keys == LEGACY_STAMP_KEYS:
        if (
            value["schema_version"] != 1
            or value["product_name"] != PRODUCT_NAME
            or value["build_version"] not in LEGACY_BUILD_VERSIONS
        ):
            fail("legacy setup stamp is not compatible with this build")
        value["profile_id"] = None
        value["legacy"] = True
        value["needs_update"] = True
    else:
        fail(
            "setup stamp has invalid keys "
            f"(missing={sorted(STAMP_KEYS - actual_keys)}, extra={sorted(actual_keys - STAMP_KEYS)})"
        )
    if value["canonical_target"] != str(target):
        fail("setup stamp is bound to a different canonical target")
    if not isinstance(value["managed_files"], dict):
        fail("setup stamp managed_files must be an object")
    validate_setup_id(value["setup_id"])
    return value


def validate_managed_files(target: Path, stamp: dict[str, Any]) -> list[str]:
    expected = stamp["managed_files"]
    known = ALL_MANAGED_PATHS if stamp.get("legacy") else MANAGED_PATHS
    ordered = [relative for relative in known if str(relative) in expected]
    ordered.extend(Path(raw) for raw in sorted(set(expected) - {str(item) for item in ordered}))
    drift: list[str] = []
    for relative in ordered:
        if relative.is_absolute() or ".." in relative.parts:
            fail("setup stamp contains an unsafe managed path")
        content, _ = read_regular_file(
            target / relative, f"managed file {relative}", owner_only=True
        )
        digest = legacy_managed_digest(relative, content) if stamp.get("legacy") else managed_digest(relative, content)
        if digest != expected[str(relative)]:
            drift.append(str(relative))
    if drift:
        fail(f"managed target drift detected: {', '.join(sorted(drift))}")
    return sorted(expected)


def inspect_target(target: Path) -> dict[str, Any]:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {"state": "missing", "target": str(target), "cleanup_pending": False}
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
        return {"state": "unmanaged", "target": str(target), **cleanup_pending_metadata(target)}
    state_name = "legacy-managed" if stamp.get("legacy") else "managed"
    result = {
        "state": state_name,
        "target": str(target),
        "setup_id": stamp["setup_id"],
        "profile_id": stamp["profile_id"],
        "build_version": stamp["build_version"],
        "managed_files": validate_managed_files(target, stamp),
        "builder_projection": stamp["builder_projection"],
        "launch_args": stamp["launch_args"],
        "command_permissions": stamp["command_permissions"],
        "needs_update": stamp["needs_update"],
        **cleanup_pending_metadata(target),
    }
    if stamp.get("legacy"):
        result["launch_supported"] = False
        result["migration_required"] = True
    return result


def read_existing_settings_if_managed(target: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    if state.get("state") != "managed":
        return None
    return load_json_object(
        target / CLINE_GLOBAL_SETTINGS_RELATIVE,
        f"existing {CLINE_GLOBAL_SETTINGS_RELATIVE}",
        owner_only=True,
    )


def current_managed_snapshot(target: Path, paths: tuple[Path, ...] = ALL_MANAGED_PATHS) -> dict[Path, tuple[int, int] | None]:
    snapshot: dict[Path, tuple[int, int] | None] = {}
    for relative in paths:
        path = target / relative
        if path.exists() or path.is_symlink():
            info = require_regular_file(path, f"managed file {relative}", owner_only=True)
            snapshot[relative] = identity_of(info)
        else:
            snapshot[relative] = None
    return snapshot


def prune_empty_managed_dirs(target: Path, paths: tuple[Path, ...] = ALL_MANAGED_PATHS) -> None:
    candidates = sorted(
        {(target / relative).parent for relative in paths},
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


def restore_snapshot(target: Path, snapshot: dict[Path, tuple[int, int] | None]) -> None:
    del target, snapshot
    fail("managed rollback requires held original objects and must not recreate files from bytes")


def replace_managed_state(
    target: Path, desired: dict[Path, bytes | None], expected: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    if expected.get("state") in {"managed", "legacy-managed"}:
        stamp = load_stamp(target)
        if stamp is None:
            fail_concurrent("managed state disappeared before replacement")
        validate_managed_files(target, stamp)
    transaction_root = target / MANAGER_CONTROL_RELATIVE / "managed-transactions" / f"txn-{os.getpid()}-{time.time_ns()}"
    hold_root = transaction_root / "held"
    stage_root = transaction_root / "stage"
    hold_root.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    stage_root.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    moved_old: list[Path] = []
    installed_new: list[Path] = []
    created_parents = sorted(
        {(target / relative).parent for relative in desired},
        key=lambda item: len(item.parts),
        reverse=True,
    )
    try:
        for relative, content in desired.items():
            if content is None:
                continue
            staged = ensure_private_parent(stage_root, relative)
            write_regular_file_atomic(staged, content, OWNER_FILE_MODE, f"staged managed file {relative}")
        for relative in desired:
            destination = target / relative
            if path_present(destination):
                require_regular_file(destination, f"managed file {relative}", owner_only=True)
                held = ensure_private_parent(hold_root, relative)
                os.replace(destination, held)
                moved_old.append(relative)
                sync_directory(destination.parent, f"managed file {relative} parent")
                sync_directory(held.parent, f"held managed file {relative} parent")
        for relative, content in desired.items():
            if content is None:
                continue
            destination = ensure_private_parent(target, relative)
            os.replace(stage_root / relative, destination)
            destination.chmod(OWNER_FILE_MODE)
            installed_new.append(relative)
            sync_directory(destination.parent, f"managed file {relative} parent")
        prune_empty_managed_dirs(target, tuple(desired))
        post_state = inspect_target(target)
        cleanup_created = publish_cleanup_pending_for_paths(
            target,
            [hold_root],
            reason="managed-replacement",
        )
    except BaseException:
        for relative in reversed(installed_new):
            destination = target / relative
            if path_present(destination):
                require_regular_file(destination, f"new managed file {relative}", owner_only=True)
                destination.unlink()
                sync_directory(destination.parent, f"managed rollback parent {relative}")
        for relative in reversed(moved_old):
            held = hold_root / relative
            if path_present(held):
                destination = ensure_private_parent(target, relative)
                os.replace(held, destination)
                sync_directory(destination.parent, f"managed rollback parent {relative}")
        prune_empty_managed_dirs(target, tuple(desired))
        with contextlib.suppress(OSError):
            cleanup_path(transaction_root)
        raise
    with contextlib.suppress(OSError):
        cleanup_path(stage_root)
    with contextlib.suppress(OSError):
        transaction_root.rmdir()
    cleanup_pending = False
    if cleanup_created:
        try:
            drain_cleanup_pending(target)
        except Exception:
            cleanup_pending = True
    for directory in created_parents:
        if directory == target:
            continue
        with contextlib.suppress(OSError):
            if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
                directory.rmdir()
    return cleanup_pending, post_state


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
    ):
        fail("backup pool marker schema or product identity is not compatible with this manager")
    if not isinstance(marker["build_version"], str):
        fail("backup pool marker build_version must be a string")
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
    if path_present(pool):
        require_private_directory(pool, "backup pool")
        validate_backup_pool_marker(target, pool)
        return pool
    legacy_pool = legacy_backup_pool(target)
    if path_present(legacy_pool):
        require_private_directory(target.parent, "legacy backup pool parent")
        require_private_directory(legacy_pool, "legacy backup pool")
        validate_backup_pool_marker(target, legacy_pool)
        return legacy_pool
    fail("backup pool is missing")


def ensure_backup_pool(target: Path) -> Path:
    pool = backup_pool(target)
    try:
        info = pool.lstat()
    except FileNotFoundError:
        require_private_directory(pool.parent, "backup pool parent")
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
        envelope["schema_version"] != 2
        or envelope["product_name"] != PRODUCT_NAME
    ):
        fail(f"{label} schema or product identity is not compatible with this manager")
    if not isinstance(envelope["build_version"], str):
        fail(f"{label} build_version must be a string")
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
    file_records = envelope["files"]
    expected_payloads = [*raw_files, STAMP_NAME]
    if not isinstance(file_records, list) or len(file_records) != len(expected_payloads):
        fail(f"{label} file records are invalid")
    seen_payloads: set[str] = set()
    for record in file_records:
        if not isinstance(record, dict):
            fail(f"{label} file record must be an object")
        require_exact_keys(record, {"path", "size", "sha256"}, f"{label} file record")
        raw_path = record["path"]
        if raw_path not in expected_payloads or raw_path in seen_payloads:
            fail(f"{label} file record path is invalid")
        seen_payloads.add(raw_path)
        if not isinstance(record["size"], int) or record["size"] < 0:
            fail(f"{label} file record size is invalid")
        if not isinstance(record["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
            fail(f"{label} file record digest is invalid")
    if seen_payloads != set(expected_payloads):
        fail(f"{label} file record set is incomplete")


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
    allowed_files = {BACKUP_NAME, *[*envelope["managed_files"], STAMP_NAME]}
    observed_files: set[str] = set()
    for child in sorted(slot_dir.rglob("*"), key=lambda item: str(item.relative_to(slot_dir))):
        relative = str(child.relative_to(slot_dir))
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail(f"backup slot {slot} contains an unsafe entry")
        if relative not in allowed_files:
            fail(f"backup slot {slot} contains an unrecorded entry")
        observed_files.add(relative)
    if observed_files != allowed_files:
        fail(f"backup slot {slot} file set does not match its envelope")
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
    slots = backup_slots_for_rotation(target, pool)
    retired_paths = [pool / "9"] if 9 in slots else []
    cleanup_created = publish_cleanup_pending_for_paths(
        target,
        retired_paths,
        reason="backup-rotation",
    )
    if cleanup_created:
        drain_cleanup_pending(target)
    for slot in sorted((slot for slot in slots if slot != 9), reverse=True):
        current = pool / str(slot)
        os.replace(current, pool / str(slot + 1))
    stage_dir = pool / f".stage-0-{os.getpid()}-{time.time_ns()}"
    stage_dir.mkdir(mode=OWNER_DIRECTORY_MODE)
    stage_dir.chmod(OWNER_DIRECTORY_MODE)
    published = False
    try:
        managed_files = list(state["managed_files"])
        file_records: list[dict[str, Any]] = []
        for raw_relative in [*managed_files, STAMP_NAME]:
            relative = Path(raw_relative)
            content, _ = read_regular_file(
                target / relative, f"managed file {relative}", owner_only=True
            )
            destination = ensure_private_parent(stage_dir, relative)
            destination.write_bytes(content)
            destination.chmod(OWNER_FILE_MODE)
            file_records.append(
                {
                    "path": str(relative),
                    "size": len(content),
                    "sha256": sha256_bytes(content),
                }
            )
        envelope = {
            "schema_version": 2,
            "product_name": PRODUCT_NAME,
            "build_version": VERSION,
            "slot": 0,
            "canonical_target": str(target),
            "source_setup_id": state["setup_id"],
            "managed_files": managed_files,
            "files": file_records,
            "created_at": int(time.time()),
        }
        (stage_dir / BACKUP_NAME).write_bytes(canonical_json(envelope))
        (stage_dir / BACKUP_NAME).chmod(OWNER_FILE_MODE)
        load_backup_envelope(target, stage_dir, 0, expected_slot=0)
        os.replace(stage_dir, pool / "0")
        published = True
    finally:
        if not published:
            with contextlib.suppress(OSError):
                cleanup_path(stage_dir)
    sync_directory(pool, "backup pool")
    refresh_backup_slot_numbers(target, pool)
    return 0


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[Path, bytes]]:
    if slot < 0 or slot > 9:
        fail("--backup must be between 0 and 9")
    pool = require_backup_pool(target)
    slot_dir = pool / str(slot)
    envelope = load_backup_envelope(target, slot_dir, slot, expected_slot=slot)
    file_records = {record["path"]: record for record in envelope["files"]}
    files: dict[Path, bytes] = {}
    for raw_relative in [*envelope["managed_files"], STAMP_NAME]:
        relative = Path(raw_relative)
        if relative.is_absolute() or ".." in relative.parts:
            fail("backup contains an unsafe managed file path")
        content, _ = read_regular_file(
            slot_dir / relative, f"backup file {relative}", owner_only=True
        )
        record = file_records[str(relative)]
        if record["size"] != len(content) or record["sha256"] != sha256_bytes(content):
            fail(f"backup file {relative} digest mismatch")
        files[relative] = content
    return envelope, files


def backup_pool_present(target: Path) -> bool:
    return path_present(backup_pool(target)) or path_present(legacy_backup_pool(target))


def rollback_created_backup_pool(
    target: Path,
    *,
    existed_before: bool,
    target_metadata: dict[str, Any] | None,
) -> None:
    if existed_before:
        return
    pool = backup_pool(target)
    if path_present(pool):
        remove_tree_no_follow(pool)
        sync_directory(target, "backup rollback target")
    restore_directory_metadata(target, target_metadata)


def mutate_setup(target: Path, setup_id: str, profile_id: str, operation: str) -> dict[str, Any]:
    with locked_new_or_existing_target_with_creation(
        target
    ) as (canonical_target, created_target, parent_snapshot):
        cleanup_drained = drain_or_recover_cleanup_before_mutation(canonical_target)
        state = inspect_target(canonical_target)
        if state["state"] == "legacy-managed":
            fail("legacy managed targets must be migrated, restored, or removed before launch")
        existing_settings = read_existing_settings_if_managed(canonical_target, state)
        metadata, desired = render_setup(
            setup_id,
            profile_id,
            existing_settings=existing_settings,
        )
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        changed = changed_paths(canonical_target, desired)
        backup_slot: int | None = None
        backup_existed = backup_pool_present(canonical_target)
        backup_target_metadata = directory_metadata(canonical_target) if changed else None
        cleanup_pending = False
        try:
            if state["state"] == "managed" and changed:
                backup_slot = create_backup(canonical_target, state)
            if changed:
                cleanup_pending, post = replace_managed_state(canonical_target, desired, state)
            else:
                post = inspect_target(canonical_target)
        except BaseException:
            rollback_created_backup_pool(
                canonical_target,
                existed_before=backup_existed,
                target_metadata=backup_target_metadata,
            )
            if created_target:
                remove_created_target_tree(canonical_target, parent_snapshot)
            raise
    return {
        "ok": True,
        "operation": operation,
        "setup_id": setup_id,
        "profile_id": profile_id,
        "description": metadata["description"],
        "target": str(canonical_target),
        "changed": changed,
        "backup_slot": backup_slot,
        "state": post["state"],
        "cleanup_drained": cleanup_drained,
        "cleanup_pending": cleanup_pending,
    }


def plan_setup(target: Path, setup_id: str, profile_id: str) -> dict[str, Any]:
    with locked_inspection_target(target) as canonical_target:
        state = inspect_target(canonical_target)
        existing_settings = read_existing_settings_if_managed(canonical_target, state)
        _metadata, desired = render_setup(
            setup_id,
            profile_id,
            existing_settings=existing_settings,
        )
        if state["state"] == "managed":
            stamp = bind_stamp(
                parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
            )
            desired[Path(STAMP_NAME)] = canonical_json(stamp)
            changed = changed_paths(canonical_target, desired)
            operation = (
                "switch"
                if state.get("setup_id") != setup_id or state.get("profile_id") != profile_id
                else "install"
            )
            backup_required = bool(changed)
        elif state["state"] == "legacy-managed":
            changed = sorted(str(path) for path in desired)
            operation = "migrate"
            backup_required = True
        else:
            changed = sorted(str(path) for path in desired)
            operation = "install"
            backup_required = False
        return {
            "ok": True,
            "operation": operation,
            "setup_id": setup_id,
            "profile_id": profile_id,
            "target": str(canonical_target),
            "state": state["state"],
            "cleanup_pending": state.get("cleanup_pending", False),
            "mutates": False,
            "backup_required": backup_required,
            "changed": changed,
        }


def migrate_setup(target: Path, setup_id: str, profile_id: str) -> dict[str, Any]:
    with locked_existing_target(target) as canonical_target:
        cleanup_drained = drain_or_recover_cleanup_before_mutation(canonical_target)
        state = inspect_target(canonical_target)
        if state["state"] != "legacy-managed":
            fail("target does not contain legacy nddev-cline-app managed state")
        metadata, desired = render_setup(setup_id, profile_id, existing_settings=None)
        stamp = bind_stamp(
            parse_json_object(desired[Path(STAMP_NAME)], "desired stamp"), canonical_target
        )
        desired[Path(STAMP_NAME)] = canonical_json(stamp)
        for raw_relative in state["managed_files"]:
            relative = Path(raw_relative)
            if relative != Path(STAMP_NAME) and relative not in desired:
                desired[relative] = None
        changed = changed_paths(canonical_target, desired)
        backup_existed = backup_pool_present(canonical_target)
        backup_target_metadata = directory_metadata(canonical_target)
        cleanup_pending = False
        try:
            backup_slot = create_backup(canonical_target, state)
            cleanup_pending, post = replace_managed_state(canonical_target, desired, state)
        except BaseException:
            rollback_created_backup_pool(
                canonical_target,
                existed_before=backup_existed,
                target_metadata=backup_target_metadata,
            )
            raise
    return {
        "ok": True,
        "operation": "migrate",
        "setup_id": setup_id,
        "profile_id": profile_id,
        "description": metadata["description"],
        "target": str(canonical_target),
        "changed": changed,
        "backup_slot": backup_slot,
        "state": post["state"],
        "cleanup_drained": cleanup_drained,
        "cleanup_pending": cleanup_pending,
    }


def remove_setup(target: Path) -> dict[str, Any]:
    with locked_existing_target(target) as canonical_target:
        cleanup_drained = drain_or_recover_cleanup_before_mutation(canonical_target)
        state = inspect_target(canonical_target)
        if state["state"] not in {"managed", "legacy-managed"}:
            fail("target is not managed by nddev-cline-app")
        state_paths = tuple(Path(raw) for raw in [*state["managed_files"], STAMP_NAME])
        desired = {relative: None for relative in state_paths}
        backup_existed = backup_pool_present(canonical_target)
        backup_target_metadata = directory_metadata(canonical_target)
        cleanup_pending = False
        try:
            backup_slot = create_backup(canonical_target, state)
            cleanup_pending, _post = replace_managed_state(canonical_target, desired, state)
        except BaseException:
            rollback_created_backup_pool(
                canonical_target,
                existed_before=backup_existed,
                target_metadata=backup_target_metadata,
            )
            raise
    return {
        "ok": True,
        "operation": "remove",
        "target": str(canonical_target),
        "removed_setup_id": state["setup_id"],
        "backup_slot": backup_slot,
        "cleanup_drained": cleanup_drained,
        "cleanup_pending": cleanup_pending,
    }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    with locked_existing_target(target) as canonical_target:
        cleanup_drained = drain_or_recover_cleanup_before_mutation(canonical_target)
        state = inspect_target(canonical_target)
        if state["state"] not in {"managed", "legacy-managed"}:
            fail("target is not managed by nddev-cline-app")
        envelope, desired = load_backup(canonical_target, slot)
        for raw_relative in [*state["managed_files"], STAMP_NAME]:
            relative = Path(raw_relative)
            if relative not in desired:
                desired[relative] = None
        cleanup_pending = False
        try:
            cleanup_pending, post = replace_managed_state(canonical_target, desired, state)
        except BaseException:
            raise
    return {
        "ok": True,
        "operation": "restore",
        "target": str(canonical_target),
        "setup_id": post["setup_id"],
        "profile_id": post["profile_id"],
        "state": post["state"],
        "restored_from_slot": slot,
        "restored_source_setup_id": envelope["source_setup_id"],
        "cleanup_drained": cleanup_drained,
        "cleanup_pending": cleanup_pending,
    }


def load_baseline() -> dict[str, Any]:
    return load_json_object(BASELINE_REF, "Cline baseline")


def lockfile_sha256() -> str:
    content, _ = read_regular_file(
        INSTALL_PACKAGE_LOCK,
        "Cline CLI package-lock.json",
        max_bytes=METADATA_MAX_BYTES,
    )
    return sha256_bytes(content)


def validate_install_lock_contract() -> None:
    baseline = load_baseline()
    expected_lock = baseline.get("package_manager", {}).get("lockfile_sha256")
    if not isinstance(expected_lock, str) or lockfile_sha256() != expected_lock:
        fail("Cline CLI package-lock digest does not match the pinned baseline")
    package_json = load_json_object(INSTALL_PACKAGE_JSON, "Cline CLI package.json")
    package_lock = load_json_object(INSTALL_PACKAGE_LOCK, "Cline CLI package-lock.json")
    dependencies = package_json.get("dependencies")
    if dependencies != {NPM_PACKAGE: TESTED_CLI_VERSION}:
        fail("Cline CLI package.json must pin the exact Cline dependency")
    packages = package_lock.get("packages")
    if package_lock.get("lockfileVersion") != 3 or not isinstance(packages, dict):
        fail("Cline CLI package-lock.json must be lockfileVersion 3 with packages")
    root = packages.get("")
    if not isinstance(root, dict) or root.get("dependencies") != dependencies:
        fail("Cline CLI package-lock root dependency mismatch")
    cline_package = packages.get("node_modules/cline")
    if not isinstance(cline_package, dict):
        fail("Cline CLI package-lock missing cline package metadata")
    if cline_package.get("version") != TESTED_CLI_VERSION:
        fail("Cline CLI package-lock cline package version mismatch")
    if cline_package.get("bin") != {COMMAND_NAME: "bin/cline"}:
        fail("Cline CLI package wrapper bin mapping mismatch")
    if cline_package.get("optionalDependencies") != {
        package: TESTED_CLI_VERSION for package in EXPECTED_CLINE_OPTIONAL_PACKAGES
    }:
        fail("Cline CLI package optional native dependency mapping mismatch")
    optional_seen: set[str] = set()
    for path, metadata in packages.items():
        if not isinstance(path, str) or not isinstance(metadata, dict):
            fail("Cline CLI package-lock packages must be objects")
        if path == "":
            continue
        name = path.removeprefix("node_modules/")
        if name in EXPECTED_CLINE_OPTIONAL_PACKAGES:
            optional_seen.add(name)
            if metadata.get("version") != TESTED_CLI_VERSION:
                fail(f"Cline CLI optional package {name} version mismatch")
            native_contract = SUPPORTED_NATIVE_OPTIONAL_PACKAGES.get(name)
            if native_contract is not None:
                if metadata.get("optional") is not True:
                    fail(f"Cline CLI optional package {name} must be marked optional")
                if metadata.get("os") != native_contract["os"]:
                    fail(f"Cline CLI optional package {name} os selector mismatch")
                if metadata.get("cpu") != native_contract["cpu"]:
                    fail(f"Cline CLI optional package {name} cpu selector mismatch")
                if metadata.get("bin") != {COMMAND_NAME: native_contract["bin"]}:
                    fail(f"Cline CLI optional package {name} bin mapping mismatch")
        resolved = metadata.get("resolved")
        if isinstance(resolved, str):
            if not resolved.startswith(NPM_REGISTRY):
                fail(f"Cline CLI package-lock has non-registry resolution for {path}")
        elif metadata.get("link") is not True:
            fail(f"Cline CLI package-lock package {path} is missing resolved")
        if metadata.get("link") is not True and not isinstance(metadata.get("integrity"), str):
            fail(f"Cline CLI package-lock package {path} is missing integrity")
    if optional_seen != EXPECTED_CLINE_OPTIONAL_PACKAGES:
        fail("Cline CLI package-lock missing expected optional platform packages")


def software_manifest_path(target: Path) -> Path:
    return target / SOFTWARE_MANIFEST_RELATIVE


def cline_executable(target: Path) -> Path:
    return target / "bin" / COMMAND_NAME


def require_supported_runtime_platform() -> None:
    current_product_host_id()


def current_machine_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"x86_64", "amd64"}:
        return "x64"
    fail(f"{PRODUCT_NAME} {VERSION} does not support this CPU architecture: {machine}")


def current_linux_distribution() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        content = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"{PRODUCT_NAME} {VERSION} requires Ubuntu on Linux; cannot read /etc/os-release: {exc}")
    for line in content.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value.strip().strip('"')
    return data


def current_product_host_id() -> str:
    arch = current_machine_arch()
    if sys.platform.startswith("darwin"):
        return f"macos-{arch}"
    if sys.platform.startswith("linux"):
        distro = current_linux_distribution()
        distro_id = distro.get("ID", "").lower()
        id_like = {item.lower() for item in distro.get("ID_LIKE", "").split()}
        if distro_id != "ubuntu" and "ubuntu" not in id_like:
            fail(f"{PRODUCT_NAME} {VERSION} supports Cline CLI launch/install only on Ubuntu glibc hosts")
        libc_name, _libc_version = platform.libc_ver()
        if libc_name and libc_name.lower() != "glibc":
            fail(f"{PRODUCT_NAME} {VERSION} supports Cline CLI launch/install only on Ubuntu glibc hosts")
        return f"ubuntu-glibc-{arch}"
    fail(f"{PRODUCT_NAME} {VERSION} supports Cline CLI launch/install only on macOS and Ubuntu glibc")


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_child_base_environment() -> dict[str, str]:
    env = {"PATH": DETERMINISTIC_PATH}
    for name in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "ComSpec"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def trusted_executable(name: str) -> str:
    resolved = shutil.which(name, path=DETERMINISTIC_PATH)
    if resolved is None:
        fail(f"{name} executable was not found on the trusted tool path")
    path = Path(resolved)
    if not path.is_absolute():
        fail(f"{name} executable resolution was not absolute")
    return str(path)


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


def launch_handoff_paths(target: Path) -> tuple[Path, ...]:
    package_wrapper = target / PACKAGE_WRAPPER_RELATIVE
    directories = (
        target / "bin",
        target / "software",
        target / "software" / "cline-cli",
        target / "software" / "cline-cli" / "install",
        target / "software" / "cline-cli" / "install" / "project",
        target / "software" / "cline-cli" / "install" / "project" / "node_modules",
        package_wrapper.parents[2],
        package_wrapper.parents[1],
        package_wrapper.parent,
    )
    files = (target / "bin" / COMMAND_NAME, package_wrapper)
    return tuple(dict.fromkeys((*directories, *files)))


@contextlib.contextmanager
def protected_launch_handoff(target: Path) -> Iterator[None]:
    """Temporarily remove owner-write bits from the path-exec handoff chain."""
    records: list[tuple[Path, tuple[int, int], int]] = []
    for path in launch_handoff_paths(target):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"launch handoff path {path.relative_to(target)} must not be a symlink")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            fail(f"launch handoff path {path.relative_to(target)} has an unsafe type")
        if hasattr(os, "geteuid") and owner_of(info) != os.geteuid():
            fail(f"launch handoff path {path.relative_to(target)} must be owned by the current user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            fail(f"launch handoff path {path.relative_to(target)} must not be group/other accessible")
        records.append((path, identity_of(info), stat.S_IMODE(info.st_mode)))
    try:
        for path, _identity, _mode in records:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                path.chmod(PROTECTED_DIRECTORY_MODE)
            elif stat.S_ISREG(info.st_mode):
                path.chmod(PROTECTED_EXECUTABLE_MODE)
        yield
    finally:
        for path, expected_identity, original_mode in reversed(records):
            with contextlib.suppress(FileNotFoundError, OSError):
                info = path.lstat()
                if identity_of(info) == expected_identity:
                    path.chmod(original_mode)


def revalidate_launch_handoff(target: Path, manifest: dict[str, Any]) -> None:
    require_safe_executable(
        cline_executable(target),
        target,
        "Cline CLI executable",
        allow_hardlinks=True,
    )
    entrypoint = resolve_target_owned_path(
        cline_executable(target), target, "Cline CLI executable"
    )
    entrypoint_digest = digest_regular_file(
        entrypoint, "Cline CLI executable", {"value": 0}, allow_hardlinks=True
    )
    package_wrapper = target / PACKAGE_WRAPPER_RELATIVE
    package_wrapper_digest = digest_regular_file(
        package_wrapper, "Cline package wrapper", {"value": 0}, allow_hardlinks=False
    )
    if entrypoint_digest != manifest.get("entrypoint_sha256"):
        fail("Cline CLI executable digest changed before launch")
    if package_wrapper_digest != manifest.get("package_wrapper_sha256"):
        fail("Cline package wrapper digest changed before launch")


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
        "install_method": "npm-ci-lockfile",
        "package_manager": "npm",
        "package": NPM_PACKAGE,
        "package_spec": f"{NPM_PACKAGE}@{version}",
        "version": version,
        "executable": f"bin/{COMMAND_NAME}",
        "project_dir": str(INSTALL_PROJECT_RELATIVE),
        "lockfile": str(INSTALL_PACKAGE_LOCK.relative_to(ROOT)),
        "lockfile_sha256": lockfile_sha256(),
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


def cleanup_parent(target: Path) -> Path:
    return target / CLEANUP_PARENT_RELATIVE


def cleanup_tombstones(target: Path) -> Path:
    return target / CLEANUP_TOMBSTONES_RELATIVE


def cleanup_pending_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_PENDING_NAME


def cleanup_intent_paths(target: Path) -> list[Path]:
    parent = cleanup_parent(target)
    if not path_present(parent):
        return []
    return sorted(
        (child for child in parent.iterdir() if child.name.startswith(CLEANUP_INTENT_PREFIX)),
        key=lambda item: item.name,
    )


def relative_under(path: Path, parent: Path, label: str) -> Path:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        fail(f"{label} escapes its declared parent")
    if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
        fail(f"{label} has an unsafe relative path")
    return relative


def directory_metadata(path: Path) -> dict[str, Any]:
    info = require_directory(path, "directory metadata snapshot")
    return {
        "mode": stat.S_IMODE(info.st_mode),
        "atime_ns": info.st_atime_ns,
        "mtime_ns": info.st_mtime_ns,
        "dev": info.st_dev,
        "ino": info.st_ino,
    }


def restore_directory_metadata(path: Path, snapshot: dict[str, Any] | None) -> None:
    if snapshot is None:
        return
    info = require_directory(path, "directory metadata restore")
    if identity_of(info) != (snapshot["dev"], snapshot["ino"]):
        fail("directory identity changed during rollback")
    os.chmod(path, int(snapshot["mode"]))
    os.utime(path, ns=(int(snapshot["atime_ns"]), int(snapshot["mtime_ns"])), follow_symlinks=False)


def remove_created_target_tree(target: Path, parent_snapshot: dict[str, Any] | None) -> None:
    if path_present(target):
        make_tree_owner_writable(target)
        remove_tree_no_follow(target)
        sync_directory(target.parent, "created target parent")
    restore_directory_metadata(target.parent, parent_snapshot)


def make_tree_owner_writable(root: Path) -> None:
    if not path_present(root):
        return
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        paths.extend(sorted(root.rglob("*"), key=lambda item: len(item.parts)))
    for path in paths:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail("rollback tree must not contain symlinks")
        if stat.S_ISDIR(info.st_mode):
            os.chmod(path, OWNER_DIRECTORY_MODE)
        elif stat.S_ISREG(info.st_mode):
            os.chmod(path, OWNER_FILE_MODE if not (stat.S_IMODE(info.st_mode) & stat.S_IXUSR) else OWNER_EXECUTABLE_MODE)
        else:
            fail("rollback tree must not contain special files")


def cleanup_entry_snapshot(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not path_present(root):
        fail("cleanup tombstone is missing")
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        paths.extend(sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))))
    if len(paths) > CLEANUP_MAX_ENTRIES:
        fail("cleanup tombstone exceeds the entry bound")
    total_bytes = 0
    for path in paths:
        info = path.lstat()
        relative = "." if path == root else str(path.relative_to(root))
        mode = stat.S_IMODE(info.st_mode)
        record: dict[str, Any] = {
            "relative": relative,
            "mode": mode,
            "uid": owner_of(info),
            "nlink": info.st_nlink,
            "dev": info.st_dev,
            "ino": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            record["kind"] = "directory"
        elif stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if info.st_nlink != 1:
                fail("cleanup tombstone file must not have hard-link aliases")
            content, _ = read_regular_file(path, f"cleanup tombstone file {relative}")
            total_bytes += len(content)
            if total_bytes > SOFTWARE_TREE_MAX_BYTES:
                fail("cleanup tombstone exceeds the byte bound")
            record["kind"] = "file"
            record["sha256"] = sha256_bytes(content)
        elif stat.S_ISLNK(info.st_mode):
            fail("cleanup tombstone must not contain symlinks")
        else:
            fail("cleanup tombstone must not contain special files")
        entries.append(record)
    return entries


def validate_cleanup_snapshot_schema(snapshot: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(snapshot, list) or not snapshot:
        fail(f"{label} snapshot is invalid")
    if len(snapshot) > CLEANUP_MAX_ENTRIES:
        fail(f"{label} snapshot exceeds the entry bound")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in snapshot:
        if not isinstance(record, dict):
            fail(f"{label} snapshot entry must be an object")
        kind = record.get("kind")
        expected_keys = {
            "relative",
            "kind",
            "mode",
            "uid",
            "nlink",
            "dev",
            "ino",
            "size",
            "mtime_ns",
        }
        if kind == "file":
            expected_keys.add("sha256")
        require_exact_keys(record, expected_keys, f"{label} snapshot entry")
        relative = record["relative"]
        if not isinstance(relative, str) or relative in seen:
            fail(f"{label} snapshot relative path is invalid")
        if relative != ".":
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts or relative_path == Path("."):
                fail(f"{label} snapshot relative path is unsafe")
        seen.add(relative)
        if kind not in {"directory", "file"}:
            fail(f"{label} snapshot kind is invalid")
        for key in ("mode", "uid", "nlink", "dev", "ino", "size", "mtime_ns"):
            if not isinstance(record[key], int) or record[key] < 0:
                fail(f"{label} snapshot {key} is invalid")
        if kind == "file":
            if record["nlink"] != 1:
                fail(f"{label} snapshot file must not have hard-link aliases")
            if not isinstance(record["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"]):
                fail(f"{label} snapshot digest is invalid")
        result.append(record)
    if "." not in seen:
        fail(f"{label} snapshot root entry is missing")
    return result


def cleanup_snapshot_map(snapshot: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    records = validate_cleanup_snapshot_schema(snapshot, label)
    return {str(record["relative"]): record for record in records}


def cleanup_source_allowed(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        return False
    parts = relative.parts
    if parts[:2] == (".nddev-cline-control", "managed-transactions"):
        return True
    if parts[:2] == (".nddev-cline-control", "software-transactions"):
        return True
    if len(parts) == 2 and parts[0] == ".nddev-cline-backups" and parts[1].isdigit():
        slot = int(parts[1])
        return 0 <= slot <= 9
    return False


def validate_cleanup_record(
    path: Path,
    record: dict[str, Any],
    label: str,
    *,
    strict_directory_metadata: bool,
) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    kind = record["kind"]
    if kind == "file":
        if not stat.S_ISREG(info.st_mode):
            fail(f"{label} kind changed")
        if info.st_nlink != 1:
            fail(f"{label} must not have hard-link aliases")
        content, _ = read_regular_file(path, label)
        observed: dict[str, Any] = {
            "kind": "file",
            "mode": stat.S_IMODE(info.st_mode),
            "uid": owner_of(info),
            "nlink": info.st_nlink,
            "dev": info.st_dev,
            "ino": info.st_ino,
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "sha256": sha256_bytes(content),
        }
    elif kind == "directory":
        if not stat.S_ISDIR(info.st_mode):
            fail(f"{label} kind changed")
        observed = {
            "kind": "directory",
            "mode": stat.S_IMODE(info.st_mode),
            "uid": owner_of(info),
            "dev": info.st_dev,
            "ino": info.st_ino,
        }
        if strict_directory_metadata:
            observed.update(
                {
                    "nlink": info.st_nlink,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                }
            )
    else:
        fail(f"{label} snapshot kind is invalid")
    for key, value in observed.items():
        if record[key] != value:
            fail(f"{label} identity changed")


def validate_cleanup_tree_against_snapshot(
    root: Path,
    snapshot: list[dict[str, Any]],
    label: str,
    *,
    allow_missing: bool,
    strict_directory_metadata: bool,
) -> None:
    records = cleanup_snapshot_map(snapshot, label)
    if not path_present(root):
        if allow_missing:
            return
        fail(f"{label} is missing")
    observed: set[str] = set()
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        paths.extend(sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))))
    for path in paths:
        relative = "." if path == root else str(path.relative_to(root))
        if relative not in records:
            fail(f"{label} contains an undeclared path")
        observed.add(relative)
        validate_cleanup_record(
            path,
            records[relative],
            f"{label} object {relative}",
            strict_directory_metadata=strict_directory_metadata,
        )
    if not allow_missing and observed != set(records):
        fail(f"{label} is incomplete")


def remove_cleanup_tree_from_snapshot(root: Path, snapshot: list[dict[str, Any]], label: str) -> None:
    records = cleanup_snapshot_map(snapshot, label)
    validate_cleanup_tree_against_snapshot(
        root,
        snapshot,
        label,
        allow_missing=True,
        strict_directory_metadata=False,
    )
    for relative in sorted(records, key=lambda item: len(Path(item).parts), reverse=True):
        path = root if relative == "." else root / relative
        if not path_present(path):
            continue
        record = records[relative]
        validate_cleanup_record(
            path,
            record,
            f"{label} object {relative}",
            strict_directory_metadata=False,
        )
        if record["kind"] == "file":
            path.unlink()
        elif record["kind"] == "directory":
            undeclared = [
                child.name
                for child in path.iterdir()
                if str(child.relative_to(root)) not in records
            ]
            if undeclared:
                fail(f"{label} directory contains an undeclared child")
            path.rmdir()


def cleanup_journal_payload(
    target: Path,
    *,
    reason: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": CLEANUP_SCHEMA_VERSION,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": str(CLEANUP_PARENT_RELATIVE),
        "reason": reason,
        "entries": entries,
    }


def validate_cleanup_journal(target: Path, journal: dict[str, Any], label: str) -> list[dict[str, Any]]:
    require_exact_keys(
        journal,
        {
            "schema_version",
            "product_name",
            "build_version",
            "canonical_target",
            "cleanup_parent",
            "reason",
            "entries",
        },
        label,
    )
    if (
        journal["schema_version"] != CLEANUP_SCHEMA_VERSION
        or journal["product_name"] != PRODUCT_NAME
        or journal["canonical_target"] != str(target)
        or journal["cleanup_parent"] != str(CLEANUP_PARENT_RELATIVE)
    ):
        fail(f"{label} is not bound to this target")
    if not isinstance(journal["build_version"], str) or not isinstance(journal["reason"], str):
        fail(f"{label} metadata is invalid")
    entries = journal["entries"]
    if not isinstance(entries, list) or len(entries) > CLEANUP_MAX_ENTRIES:
        fail(f"{label} entries are invalid")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            fail(f"{label} entry must be an object")
        require_exact_keys(entry, {"name", "source", "destination", "snapshot"}, f"{label} entry")
        name = entry["name"]
        source = entry["source"]
        destination = entry["destination"]
        snapshot = entry["snapshot"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"tombstone-[a-f0-9]{16}", name)
            or name in seen
        ):
            fail(f"{label} tombstone name is invalid")
        seen.add(name)
        relative = Path(str(source))
        if not cleanup_source_allowed(relative):
            fail(f"{label} source path is unsafe")
        destination_relative = Path(str(destination))
        if destination_relative != CLEANUP_TOMBSTONES_RELATIVE / name:
            fail(f"{label} destination path is invalid")
        validate_cleanup_snapshot_schema(snapshot, f"{label} entry {name}")
    return entries


def load_cleanup_pending(target: Path, *, read_only: bool) -> dict[str, Any] | None:
    parent = cleanup_parent(target)
    if not path_present(parent):
        return None
    require_private_directory(parent, "cleanup parent")
    allowed = {CLEANUP_PENDING_NAME, "tombstones"}
    intent_paths: list[Path] = []
    for child in parent.iterdir():
        if child.name.startswith(CLEANUP_INTENT_PREFIX):
            intent_paths.append(child)
            continue
        if child.name.startswith(f".{CLEANUP_PENDING_NAME}.publish-"):
            fail("cleanup journal publication is incomplete")
        if child.name not in allowed:
            fail("cleanup parent contains an unmanaged path")
    pending = cleanup_pending_path(target)
    if not path_present(pending):
        if intent_paths:
            fail("cleanup recovery intent exists; run a mutating command to recover")
        tombstones = cleanup_tombstones(target)
        if path_present(tombstones) and any(tombstones.iterdir()):
            fail("cleanup tombstones exist without a pending journal")
        return None
    info = require_regular_file(pending, "cleanup pending journal", owner_only=True)
    if read_only and info.st_nlink != 1:
        fail("cleanup pending journal publication is incomplete")
    journal = load_json_object(pending, "cleanup pending journal", owner_only=True)
    entries = validate_cleanup_journal(target, journal, "cleanup pending journal")
    tombstones = cleanup_tombstones(target)
    require_private_directory(tombstones, "cleanup tombstone parent")
    expected_names = sorted(str(entry["name"]) for entry in entries)
    actual_names = sorted(child.name for child in tombstones.iterdir())
    if any(name not in expected_names for name in actual_names):
        fail("cleanup tombstone set does not match the pending journal")
    for entry in entries:
        if path_present(target / Path(str(entry["source"]))):
            fail("cleanup pending journal still has a live source path")
        tombstone = target / Path(str(entry["destination"]))
        if path_present(tombstone):
            validate_cleanup_tree_against_snapshot(
                tombstone,
                entry["snapshot"],
                "cleanup pending tombstone",
                allow_missing=True,
                strict_directory_metadata=False,
            )
    for intent_path in intent_paths:
        intent = load_cleanup_intent(target, intent_path)
        intent_entries = validate_cleanup_journal(target, intent, "cleanup recovery intent")
        if intent["reason"] != journal["reason"] or intent_entries != entries:
            fail("cleanup recovery intent does not match the pending journal")
    return journal


def cleanup_pending_metadata(target: Path) -> dict[str, Any]:
    journal = load_cleanup_pending(target, read_only=True)
    if journal is None:
        return {"cleanup_pending": False}
    return {
        "cleanup_pending": True,
        "cleanup_reason": journal["reason"],
        "cleanup_entries": len(journal["entries"]),
    }


def load_cleanup_intent(target: Path, intent_path: Path) -> dict[str, Any]:
    intent = load_json_object(intent_path, "cleanup recovery intent", owner_only=True)
    validate_cleanup_journal(target, intent, "cleanup recovery intent")
    return intent


def recover_cleanup_pending_publication_alias(target: Path) -> bool:
    pending = cleanup_pending_path(target)
    if not path_present(pending):
        return False
    info = require_regular_file(pending, "cleanup pending journal", owner_only=True)
    if info.st_nlink == 1:
        return False
    if info.st_nlink != 2:
        fail("cleanup pending journal publication is incomplete")
    parent = pending.parent
    aliases: list[Path] = []
    for child in parent.iterdir():
        if not child.name.startswith(f".{CLEANUP_PENDING_NAME}.publish-"):
            continue
        child_info = child.lstat()
        if stat.S_ISREG(child_info.st_mode) and identity_of(child_info) == identity_of(info):
            aliases.append(child)
        else:
            fail("cleanup journal has an unknown publication alias")
    if len(aliases) != 1:
        fail("cleanup journal publication is incomplete")
    aliases[0].unlink()
    sync_directory(parent, "cleanup parent")
    require_regular_file(pending, "cleanup pending journal", owner_only=True)
    return True


def recover_cleanup_intents(target: Path) -> bool:
    recovered = False
    for intent_path in cleanup_intent_paths(target):
        intent = load_cleanup_intent(target, intent_path)
        pending = cleanup_pending_path(target)
        if path_present(pending):
            journal = load_cleanup_pending(target, read_only=False)
            if journal is None or journal["entries"] != intent["entries"]:
                fail("cleanup recovery intent does not match the pending journal")
            intent_path.unlink()
            sync_directory(intent_path.parent, "cleanup parent")
            recovered = True
            continue
        for entry in reversed(intent["entries"]):
            source_relative = Path(str(entry["source"]))
            source = target / source_relative
            tombstone = target / Path(str(entry["destination"]))
            snapshot = entry["snapshot"]
            source_exists = path_present(source)
            tombstone_exists = path_present(tombstone)
            if source_exists and tombstone_exists:
                fail("cleanup recovery intent has both source and tombstone present")
            if not source_exists and tombstone_exists:
                validate_cleanup_tree_against_snapshot(
                    tombstone,
                    snapshot,
                    "cleanup recovery tombstone",
                    allow_missing=False,
                    strict_directory_metadata=True,
                )
                source.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
                os.replace(tombstone, source)
                sync_directory(source.parent, "cleanup recovery source parent")
                sync_directory(tombstone.parent, "cleanup recovery tombstone parent")
                recovered = True
            elif source_exists:
                validate_cleanup_tree_against_snapshot(
                    source,
                    snapshot,
                    "cleanup recovery source",
                    allow_missing=False,
                    strict_directory_metadata=True,
                )
            else:
                fail("cleanup recovery intent lost both source and tombstone")
        intent_path.unlink()
        sync_directory(intent_path.parent, "cleanup parent")
        recovered = True
    for directory in (cleanup_tombstones(target), cleanup_parent(target), target / MANAGER_CONTROL_RELATIVE):
        with contextlib.suppress(OSError):
            if path_present(directory) and directory.is_dir() and not directory.is_symlink():
                directory.rmdir()
                sync_directory(directory.parent, "cleanup parent")
    return recovered


def drain_or_recover_cleanup_before_mutation(target: Path) -> bool:
    recovered = recover_cleanup_intents(target)
    alias_recovered = recover_cleanup_pending_publication_alias(target)
    drained = drain_cleanup_pending(target)
    return recovered or alias_recovered or drained


def remove_tree_no_follow(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        fail("cleanup must not remove symlinks")
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            fail("cleanup file must not have hard-link aliases")
        path.unlink()
        return
    if not stat.S_ISDIR(info.st_mode):
        fail("cleanup must not remove special files")
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        remove_tree_no_follow(child)
    path.rmdir()


def drain_cleanup_pending(target: Path) -> bool:
    journal = load_cleanup_pending(target, read_only=False)
    if journal is None:
        return False
    entries = validate_cleanup_journal(target, journal, "cleanup pending journal")
    for entry in entries:
        tombstone = target / Path(str(entry["destination"]))
        if path_present(tombstone):
            remove_cleanup_tree_from_snapshot(
                tombstone,
                entry["snapshot"],
                f"cleanup tombstone {entry['name']}",
            )
            sync_directory(tombstone.parent, "cleanup tombstone parent")
    pending = cleanup_pending_path(target)
    pending.unlink()
    sync_directory(pending.parent, "cleanup parent")
    for directory in (cleanup_tombstones(target), cleanup_parent(target), target / MANAGER_CONTROL_RELATIVE):
        with contextlib.suppress(OSError):
            if path_present(directory) and directory.is_dir() and not directory.is_symlink():
                directory.rmdir()
                sync_directory(directory.parent, "cleanup parent")
    return True


def publish_cleanup_pending_for_paths(target: Path, sources: list[Path], *, reason: str) -> bool:
    existing = [source for source in sources if path_present(source)]
    if not existing:
        return False
    parent = cleanup_parent(target)
    tombstones = cleanup_tombstones(target)
    parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    parent.chmod(OWNER_DIRECTORY_MODE)
    tombstones.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    tombstones.chmod(OWNER_DIRECTORY_MODE)
    if path_present(cleanup_pending_path(target)):
        fail("cleanup pending state already exists")
    if cleanup_intent_paths(target):
        fail("cleanup recovery intent already exists")
    entries: list[dict[str, Any]] = []
    for index, source in enumerate(existing):
        source_relative = relative_under(source, target, "cleanup source")
        if not cleanup_source_allowed(source_relative):
            fail("cleanup source path is not allowlisted for this operation")
        name_seed = f"{time.time_ns()}-{os.getpid()}-{index}-{source_relative}"
        tombstone_name = f"tombstone-{hashlib.sha256(name_seed.encode('utf-8')).hexdigest()[:16]}"
        destination = CLEANUP_TOMBSTONES_RELATIVE / tombstone_name
        snapshot = cleanup_entry_snapshot(source)
        entries.append(
            {
                "name": tombstone_name,
                "source": str(source_relative),
                "destination": str(destination),
                "snapshot": snapshot,
            }
        )
    intent = cleanup_journal_payload(target, reason=reason, entries=entries)
    validate_cleanup_journal(target, intent, "cleanup recovery intent")
    intent_path = parent / f"{CLEANUP_INTENT_PREFIX}{os.getpid()}-{time.time_ns()}.json"
    publish_regular_file_no_replace(
        intent_path,
        canonical_json(intent),
        "cleanup recovery intent",
    )
    for entry in entries:
        source = target / Path(str(entry["source"]))
        tombstone = target / Path(str(entry["destination"]))
        tombstone.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        if path_present(tombstone):
            fail("cleanup tombstone destination already exists")
        validate_cleanup_tree_against_snapshot(
            source,
            entry["snapshot"],
            "cleanup source before retirement",
            allow_missing=False,
            strict_directory_metadata=True,
        )
        os.replace(source, tombstone)
        sync_directory(source.parent, "cleanup source parent")
        sync_directory(tombstone.parent, "cleanup tombstone parent")
        validate_cleanup_tree_against_snapshot(
            tombstone,
            entry["snapshot"],
            "cleanup tombstone after retirement",
            allow_missing=False,
            strict_directory_metadata=True,
        )
    journal = cleanup_journal_payload(target, reason=reason, entries=entries)
    journal_content = canonical_json(journal)
    validate_cleanup_journal(
        target,
        parse_json_object(journal_content, "cleanup pending journal"),
        "cleanup pending journal",
    )
    try:
        publish_regular_file_no_replace(
            cleanup_pending_path(target),
            journal_content,
            "cleanup pending journal",
        )
    except BaseException:
        if path_present(cleanup_pending_path(target)):
            recover_cleanup_pending_publication_alias(target)
            loaded = load_cleanup_pending(target, read_only=False)
            if loaded == journal:
                return True
        recover_cleanup_intents(target)
        raise
    intent_path.unlink()
    sync_directory(parent, "cleanup parent")
    loaded = load_cleanup_pending(target, read_only=False)
    if loaded != journal:
        fail("cleanup pending journal failed final validation")
    return True


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


def software_status(target: Path, *, recover_protected: bool = True) -> dict[str, Any]:
    del recover_protected
    try:
        info = target.lstat()
    except FileNotFoundError:
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(target),
            "cleanup_pending": False,
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
    cleanup = cleanup_pending_metadata(canonical_target)
    executable = cline_executable(canonical_target)
    manifest = software_manifest_path(canonical_target)
    presence = software_presence(canonical_target)
    if presence["software_state"] != "installed":
        return {
            "ok": True,
            "installed": False,
            "current": False,
            "target": str(canonical_target),
            **cleanup,
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
            **cleanup,
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
        **cleanup,
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


def inspect_target_with_locks(target: Path) -> dict[str, Any]:
    with locked_inspection_target(target) as canonical_target:
        return inspect_target(canonical_target)


def software_status_with_locks(target: Path) -> dict[str, Any]:
    with locked_inspection_target(target) as canonical_target:
        return software_status(canonical_target)


def write_npm_config(stage_root: Path, cache: Path) -> tuple[Path, Path]:
    userconfig = stage_root / "npmrc"
    globalconfig = stage_root / "global-npmrc"
    config = (
        f"registry={NPM_REGISTRY}\n"
        f"cache={cache}\n"
        "fund=false\n"
        "audit=false\n"
        "update-notifier=false\n"
        "ignore-scripts=true\n"
        "bin-links=false\n"
    )
    userconfig.write_text(config, encoding="utf-8")
    globalconfig.write_text("", encoding="utf-8")
    userconfig.chmod(OWNER_FILE_MODE)
    globalconfig.chmod(OWNER_FILE_MODE)
    return userconfig, globalconfig


def install_stage_environment(stage_root: Path, live_stage: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = stage_root / "home"
    cache = stage_root / "cache"
    tmp = stage_root / "tmp"
    xdg_config = stage_root / "xdg-config"
    xdg_cache = stage_root / "xdg-cache"
    xdg_state = stage_root / "xdg-state"
    project_dir = live_stage / INSTALL_PROJECT_RELATIVE
    bin_dir = live_stage / "bin"
    for directory in (
        home,
        cache,
        tmp,
        xdg_config,
        xdg_cache,
        xdg_state,
        project_dir,
        bin_dir,
    ):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
        directory.chmod(OWNER_DIRECTORY_MODE)
    userconfig, globalconfig = write_npm_config(stage_root, cache)
    env = safe_child_base_environment()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(xdg_config),
            "XDG_CACHE_HOME": str(xdg_cache),
            "XDG_STATE_HOME": str(xdg_state),
            "NPM_CONFIG_USERCONFIG": str(userconfig),
            "NPM_CONFIG_GLOBALCONFIG": str(globalconfig),
            "NPM_CONFIG_CACHE": str(cache),
            "NPM_CONFIG_REGISTRY": NPM_REGISTRY,
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_BIN_LINKS": "false",
            "NO_UPDATE_NOTIFIER": "1",
        }
    )
    return env, userconfig, globalconfig, project_dir


def seed_locked_install_project(project_dir: Path) -> None:
    for source in (INSTALL_PACKAGE_JSON, INSTALL_PACKAGE_LOCK):
        content, _ = read_regular_file(
            source,
            f"Cline CLI install asset {source.relative_to(ROOT)}",
            max_bytes=METADATA_MAX_BYTES,
        )
        destination = project_dir / source.name
        destination.write_bytes(content)
        destination.chmod(OWNER_FILE_MODE)


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
        info = None
    if info is not None and stat.S_ISLNK(info.st_mode):
        fail("staging bin/cline must not preexist as a symlink")
    if info is not None and not stat.S_ISREG(info.st_mode):
        fail("staging bin/cline must be a regular file")
    if info is not None:
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


def replace_software_state(target: Path, live_stage: Path, hold_parent: Path) -> bool:
    del hold_parent
    for relative in SOFTWARE_REPLACE_PATHS:
        source = live_stage / relative
        if not source.exists() and not source.is_symlink():
            fail(f"staged software path {relative} is missing")
        validate_replace_destination(target, relative)
    hold = target / MANAGER_CONTROL_RELATIVE / "software-transactions" / f"txn-{os.getpid()}-{time.time_ns()}"
    hold.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True)
    preexisting_parent_paths = {
        relative for relative in SOFTWARE_PARENT_PATHS if path_present(target / relative)
    }
    moved_old: list[Path] = []
    installed_new: list[Path] = []
    cleanup_pending = False
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
        cleanup_created = publish_cleanup_pending_for_paths(
            target,
            [hold],
            reason="software-replacement",
        )
        if cleanup_created:
            try:
                drain_cleanup_pending(target)
            except Exception:
                cleanup_pending = True
        else:
            with contextlib.suppress(OSError):
                cleanup_path(hold)
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
        with contextlib.suppress(OSError):
            cleanup_path(hold)
        for directory in (
            target / MANAGER_CONTROL_RELATIVE / "software-transactions",
            target / MANAGER_CONTROL_RELATIVE,
        ):
            with contextlib.suppress(OSError):
                if path_present(directory) and directory.is_dir() and not directory.is_symlink():
                    directory.rmdir()
        raise
    return cleanup_pending


def parse_node_major(version_output: str) -> int:
    match = re.search(r"v?([0-9]+)\.", version_output)
    if match is None:
        fail("node --version did not return a major version")
    return int(match.group(1))


def require_node_preflight(env: dict[str, str], stage_root: Path) -> dict[str, Any]:
    completed = run_bounded_process(
        [trusted_executable("node"), "--version"],
        cwd=stage_root,
        env=env,
        label="Node.js preflight",
    )
    if completed.returncode != 0:
        fail("node --version failed")
    version = completed.stdout.strip() or completed.stderr.strip()
    major = parse_node_major(version)
    if major < MIN_NODE_MAJOR:
        fail(
            f"Cline CLI requires Node.js {MIN_NODE_MAJOR}+ "
            f"({RECOMMENDED_NODE_MAJOR} recommended); found {version}"
        )
    return {
        "version": version,
        "major": major,
        "recommended": major >= RECOMMENDED_NODE_MAJOR,
    }


def run_npm_install(stage_root: Path, live_stage: Path) -> dict[str, Any]:
    env, userconfig, globalconfig, project_dir = install_stage_environment(stage_root, live_stage)
    validate_install_lock_contract()
    seed_locked_install_project(project_dir)
    node = require_node_preflight(env, stage_root)
    completed = run_bounded_process(
        [
            trusted_executable("npm"),
            "ci",
            "--cache",
            env["NPM_CONFIG_CACHE"],
            "--userconfig",
            str(userconfig),
            "--globalconfig",
            str(globalconfig),
            "--registry",
            NPM_REGISTRY,
            "--fund=false",
            "--audit=false",
            "--ignore-scripts=true",
            "--bin-links=false",
        ],
        cwd=project_dir,
        env=env,
        label="npm Cline CLI locked install",
    )
    if completed.returncode != 0:
        fail("npm failed to install the pinned Cline CLI")
    normalize_stage_executable(live_stage)
    chmod_private_tree(live_stage)
    observed = observed_cline_version(live_stage / "bin" / COMMAND_NAME, target=live_stage)
    if observed != TESTED_CLI_VERSION:
        fail(f"npm installed Cline CLI {observed}, expected {TESTED_CLI_VERSION}")
    return node


def write_stage_manifest(live_stage: Path) -> None:
    manifest = live_stage / SOFTWARE_MANIFEST_RELATIVE
    manifest.parent.mkdir(mode=OWNER_DIRECTORY_MODE, parents=True, exist_ok=True)
    manifest.write_bytes(canonical_json(build_software_manifest(live_stage)))
    manifest.chmod(OWNER_FILE_MODE)


def install_or_update_cli(target: Path, *, operation: str) -> dict[str, Any]:
    require_supported_runtime_platform()
    with coordinated_target(target, mutation=True, create_target_anchor=True) as locked_target:
        created_target = False
        parent_snapshot: dict[str, Any] | None = None
        canonical_target = locked_target
        try:
            if operation == "update-cli":
                try:
                    info = locked_target.lstat()
                except FileNotFoundError:
                    fail("Cline CLI is not installed; use install-cli")
                if stat.S_ISLNK(info.st_mode):
                    fail("target must not be a symlink")
                if not stat.S_ISDIR(info.st_mode):
                    fail("target must be a directory")
                if not is_owner_private_directory(info):
                    fail("target must be owned by the current user with mode 0700")
            else:
                created_target = not path_present(locked_target)
                parent_snapshot = directory_metadata(locked_target.parent) if created_target else None
                canonical_target = ensure_target_directory(locked_target)
                if canonical_target != locked_target:
                    fail_concurrent("target canonical path changed during lifecycle lock acquisition")
            cleanup_drained = drain_or_recover_cleanup_before_mutation(canonical_target)
            status = software_status(canonical_target)
            if status["installed"] and status["current"]:
                return {
                    "ok": True,
                    "operation": operation,
                    "target": str(canonical_target),
                    "version": TESTED_CLI_VERSION,
                    "package": NPM_PACKAGE,
                    "package_manager": "npm",
                    "install_method": "npm-ci-lockfile",
                    "executable": str(cline_executable(canonical_target)),
                    "changed": False,
                    "cleanup_drained": cleanup_drained,
                    "cleanup_pending": False,
                }
            with target_lock(canonical_target):
                result = install_or_update_cli_locked(
                    canonical_target,
                    operation=operation,
                    status=status,
                    cleanup_drained=cleanup_drained,
                )
        except BaseException:
            if created_target:
                remove_created_target_tree(canonical_target, parent_snapshot)
            raise
    return result


def install_or_update_cli_locked(
    target: Path,
    *,
    operation: str,
    status: dict[str, Any],
    cleanup_drained: bool,
) -> dict[str, Any]:
    canonical_target = target
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
        node = run_npm_install(staging, live_stage)
        chmod_private_tree(live_stage)
        write_stage_manifest(live_stage)
        cleanup_pending = replace_software_state(canonical_target, live_stage, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "operation": operation,
        "target": str(canonical_target),
        "version": TESTED_CLI_VERSION,
        "package": NPM_PACKAGE,
        "package_manager": "npm",
        "install_method": "npm-ci-lockfile",
        "node": node,
        "executable": str(cline_executable(canonical_target)),
        "changed": True,
        "cleanup_drained": cleanup_drained,
        "cleanup_pending": cleanup_pending,
    }


def isolated_child_environment(
    target: Path,
    *,
    profile_id: str,
    command_permissions: dict[str, Any],
) -> dict[str, str]:
    home = target / "home"
    cline_home = target / CLINE_HOME_RELATIVE
    cline_config = target / CLINE_CONFIG_RELATIVE
    hooks = target / CLINE_HOOKS_RELATIVE
    sandbox = target / CLINE_SANDBOX_RELATIVE
    runtime = target / "runtime"
    tmp = runtime / "tmp"
    for relative, label in (
        (Path("home"), "runtime directory"),
        (CLINE_HOME_RELATIVE, "runtime directory"),
        (CLINE_CONFIG_RELATIVE, "runtime directory"),
        (CLINE_HOOKS_RELATIVE, "runtime directory"),
        (Path("runtime"), "runtime directory"),
        (Path("runtime") / "tmp", "runtime directory"),
        (Path("runtime") / "xdg-config", "runtime directory"),
        (Path("runtime") / "xdg-cache", "runtime directory"),
        (Path("runtime") / "xdg-state", "runtime directory"),
    ):
        ensure_private_directory_under_target(target, relative, label)
    if profile_id == "safe":
        ensure_private_directory_under_target(target, CLINE_SANDBOX_RELATIVE, "runtime directory")
    env = safe_child_base_environment()
    for name in ("TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "CLINE_COMMAND_PERMISSIONS": json.dumps(command_permissions, sort_keys=True),
            "TMPDIR": str(tmp),
            "XDG_CONFIG_HOME": str(runtime / "xdg-config"),
            "XDG_CACHE_HOME": str(runtime / "xdg-cache"),
            "XDG_STATE_HOME": str(runtime / "xdg-state"),
            "PATH": DETERMINISTIC_PATH,
        }
    )
    if profile_id == "safe":
        env.update(
            {
                "CLINE_SANDBOX": "1",
                "CLINE_SANDBOX_DATA_DIR": str(sandbox),
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
    require_supported_runtime_platform()
    validate_launch_args(args)
    with locked_existing_target(target) as canonical_target:
        state = inspect_target(canonical_target)
        if state["state"] != "managed":
            fail("target is not managed by nddev-cline-app")
        if state.get("cleanup_pending"):
            fail("target cleanup is pending; run a mutating command before launch")
        if state.get("needs_update"):
            fail("target setup was written by a prior build; run install or switch before launch")
        profile_id = state["profile_id"]
        if profile_id not in {"safe", "full-auto"}:
            fail("target profile is not supported by this build")
        status = software_status(canonical_target)
        if not status["installed"] or not status["current"]:
            fail("Cline CLI is not installed at the tested version in this target")
        manifest = load_json_object(
            software_manifest_path(canonical_target),
            "software manifest",
            owner_only=True,
        )
        child_args = [
            *state["launch_args"],
            "--config",
            str(canonical_target / CLINE_CONFIG_RELATIVE),
            "--hooks-dir",
            str(canonical_target / CLINE_HOOKS_RELATIVE),
        ]
        if profile_id == "safe":
            child_args.extend(["--data-dir", str(canonical_target / CLINE_SANDBOX_RELATIVE)])
        child_args.extend(args)
        executable = cline_executable(canonical_target)
        child_env = isolated_child_environment(
            canonical_target,
            profile_id=profile_id,
            command_permissions=state["command_permissions"],
        )
        with protected_launch_handoff(canonical_target):
            revalidate_launch_handoff(canonical_target, manifest)
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


def add_setup_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--setup", default=DEFAULT_SETUP_ID)
    parser.add_argument("--profile", default=DEFAULT_PROFILE_ID)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="list setup variants")
    add_json_argument(list_parser)
    for name in ("status", "software-status"):
        command_parser = subparsers.add_parser(name, help=f"{name} for a target")
        add_target_argument(command_parser)
        add_json_argument(command_parser)
    for name in ("plan", "install", "switch", "migrate"):
        command_parser = subparsers.add_parser(name, help=f"{name} a setup")
        add_setup_profile_arguments(command_parser)
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
        print_payload(
            {
                "ok": True,
                "default_setup": DEFAULT_SETUP_ID,
                "default_profile": DEFAULT_PROFILE_ID,
                "setups": list_setups(),
                "profiles": list_profiles(),
            },
            json_output=args.json,
        )
        return 0
    if args.command == "status":
        target = require_absolute_target_argument(args.target)
        print_payload({"ok": True, **inspect_target_with_locks(target)}, json_output=args.json)
        return 0
    if args.command == "software-status":
        target = require_absolute_target_argument(args.target)
        print_payload(software_status_with_locks(target), json_output=args.json)
        return 0
    if args.command == "plan":
        target = require_absolute_target_argument(args.target)
        print_payload(plan_setup(target, args.setup, args.profile), json_output=args.json)
        return 0
    if args.command in {"install", "switch"}:
        target = require_absolute_target_argument(args.target)
        print_payload(
            mutate_setup(target, args.setup, args.profile, args.command),
            json_output=args.json,
        )
        return 0
    if args.command == "migrate":
        target = require_absolute_target_argument(args.target)
        print_payload(migrate_setup(target, args.setup, args.profile), json_output=args.json)
        return 0
    if args.command == "restore":
        target = require_absolute_target_argument(args.target)
        print_payload(restore_backup(target, args.backup), json_output=args.json)
        return 0
    if args.command == "remove":
        target = require_absolute_target_argument(args.target)
        print_payload(remove_setup(target), json_output=args.json)
        return 0
    if args.command == "install-cli":
        target = require_absolute_target_argument(args.target)
        print_payload(install_or_update_cli(target, operation="install-cli"), json_output=args.json)
        return 0
    if args.command == "update-cli":
        target = require_absolute_target_argument(args.target)
        print_payload(install_or_update_cli(target, operation="update-cli"), json_output=args.json)
        return 0
    if args.command == "launch":
        target = require_absolute_target_argument(args.target)
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
