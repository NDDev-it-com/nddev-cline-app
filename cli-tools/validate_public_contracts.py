#!/usr/bin/env python3
"""Validate nddev-cline-app public contracts without live Cline side effects."""

from __future__ import annotations

import contextlib
import json
import hashlib
import os
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cli-tools"))
import nddev_cline  # noqa: E402

SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+].*)?\Z")
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
SHARED_CI_COMMIT = "2ccb80e96f5771b6a6b4eae63a4f47e232906dc7"
SHARED_CI_VERSION = "0.12.0"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
SHARED_CALLERS = {
    "actionlint.yml": ".github/workflows/actionlint.yml",
    "codeql.yml": ".github/workflows/public-codeql.yml",
    "dependency-review.yml": ".github/workflows/public-dependency-review.yml",
    "release.yml": ".github/workflows/release-supply-chain.yml",
    "scorecard.yml": ".github/workflows/public-scorecard-json.yml",
    "secret-scan.yml": ".github/workflows/secret-scan.yml",
    "zizmor.yml": ".github/workflows/zizmor-sarif.yml",
}
RELEASE_ARCHIVE_PATHS = [
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "VERSION",
    "CHANGELOG.md",
    "SECURITY.md",
    ".gds/repository.yaml",
    ".github",
    "build",
    "cli-tools",
    "config",
    "plugins",
    "profiles",
    "references",
    "setups",
    "software",
]
RELEASE_RUNTIME_PATHS = [
    "README.md",
    "LICENSE",
    "VERSION",
    "build",
    "cli-tools",
    "config",
    "plugins",
    "profiles",
    "references",
    "setups",
    "software",
]
REQUIRED_RELEASE_PERMISSIONS = {
    "contents": "write",
    "id-token": "write",
    "attestations": "write",
    "artifact-metadata": "write",
}
REQUIRED_RELEASE_INPUTS = {
    "version",
    "package_name",
    "archive_paths",
    "runtime_paths",
}
REQUIRED_CONTRACT_ROOTS = {
    "build",
    "cli-tools",
    "config",
    "plugins",
    "profiles",
    "references",
    "setups",
    "software",
}
REQUIRED_GOVERNANCE_ARCHIVE_PATHS = {
    "AGENTS.md",
    ".gds/repository.yaml",
}
PRIVATE_PATH_MARKERS = (
    ".serena",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "validation",
    "tests",
    "benchmarks",
)
EXPECTED = {
    "blocked_launch_flags": [
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
    ],
}
NPM_CI_REQUIRED_FLAGS = ["--ignore-scripts=true", "--bin-links=false"]
NPM_CI_FORBIDDEN_FLAGS = ["--ignore-scripts=false", "--bin-links=true"]
FORBIDDEN_BOOTSTRAP_OVERRIDE_NAMES = (
    "NDDEV_CLINE_BOOTSTRAP_ROOT",
    "NDDEV_CLINE_LOCK_ROOT",
    "CLINE_BOOTSTRAP_LOCK_ROOT",
    "CLINE_LOCK_ROOT",
)
PLACEHOLDER_MARKER = "skele" + "ton"


def read_json(relative: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{relative} must contain a JSON object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def current_uid() -> int | str:
    if hasattr(os, "geteuid"):
        return os.geteuid()
    if hasattr(os, "getuid"):
        return os.getuid()
    return "unknown"


def bootstrap_product_root(system_root: Path) -> Path:
    return system_root / f".{nddev_cline.PRODUCT_NAME}-{current_uid()}-lifecycle-locks"


def path_identity(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
    )


def real_bootstrap_snapshot(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    snapshot: dict[str, Any] = {
        "exists": True,
        "root": path_identity(path),
        "entries": [],
    }
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return snapshot
    entries = sorted(path.iterdir(), key=lambda item: item.name)
    if len(entries) > 1000:
        snapshot["too_many_entries"] = len(entries)
        return snapshot
    for child in entries:
        child_info = child.lstat()
        record: dict[str, Any] = {
            "name": child.name,
            "identity": path_identity(child),
            "type": "other",
        }
        if stat.S_ISREG(child_info.st_mode) and child_info.st_size <= nddev_cline.METADATA_MAX_BYTES:
            record["type"] = "file"
            record["sha256"] = sha256_file(child)
        elif stat.S_ISDIR(child_info.st_mode):
            record["type"] = "directory"
        elif stat.S_ISLNK(child_info.st_mode):
            record["type"] = "symlink"
            record["target"] = os.readlink(child)
        snapshot["entries"].append(record)
    return snapshot


def write_pipe_json(fd: int, payload: dict[str, Any]) -> None:
    os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def read_pipe_json(fd: int, label: str, errors: list[str], *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        readable, _writable, _errors = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not readable:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    content = b"".join(chunks).split(b"\n", 1)[0]
    if not content:
        require(False, f"{label} did not report before timeout", errors)
        return {}
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        require(False, f"{label} reported invalid JSON: {exc}", errors)
        return {}
    if not isinstance(value, dict):
        require(False, f"{label} report must be an object", errors)
        return {}
    return value


def wait_child_success(pid: int, label: str, errors: list[str]) -> None:
    observed, status = os.waitpid(pid, 0)
    require(observed == pid, f"{label} waitpid returned the wrong child", errors)
    require(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, f"{label} failed", errors)


@contextlib.contextmanager
def isolated_bootstrap_root(errors: list[str]) -> Any:
    original = nddev_cline.fixed_system_temp_root
    real_root = original()
    real_product_root = bootstrap_product_root(real_root)
    before = real_bootstrap_snapshot(real_product_root)
    with tempfile.TemporaryDirectory(prefix="nddev-cline-bootstrap-root-") as raw:
        injected = Path(raw)
        injected.chmod(0o1777)

        def injected_fixed_system_temp_root() -> Path:
            return injected

        nddev_cline.fixed_system_temp_root = injected_fixed_system_temp_root
        try:
            yield injected
        finally:
            nddev_cline.fixed_system_temp_root = original
    after = real_bootstrap_snapshot(real_product_root)
    require(before == after, "public validator touched the real system bootstrap lock root", errors)


def expected_managed_files() -> set[str]:
    return {str(path) for path in nddev_cline.MANAGED_PATHS}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_tracked_files() -> set[str] | None:
    if not (ROOT / ".git").exists():
        return None
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if completed.returncode != 0:
        return None
    return {line for line in completed.stdout.splitlines() if line}


def _workflow_block(text: str, start: str) -> list[str]:
    lines = text.splitlines()
    start_indent = len(start) - len(start.lstrip(" "))
    for index, line in enumerate(lines):
        if line == start:
            block: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate.strip():
                    indent = len(candidate) - len(candidate.lstrip(" "))
                    if indent <= start_indent:
                        break
                if candidate and not candidate.startswith(" "):
                    break
                block.append(candidate)
            return block
    return []


def _workflow_with_value(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == f"{key}: >-":
            values: list[str] = []
            for candidate in lines[index + 1 :]:
                if not candidate.startswith("        "):
                    break
                values.extend(candidate.strip().split())
            return values
    return []


def _workflow_mapping_keys(block: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in block:
        if line.startswith("      ") and not line.startswith("        ") and ":" in line:
            keys.add(line.strip().split(":", 1)[0])
    return keys


def _workflow_scalar_mapping(block: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in block:
        if not line.startswith("      ") or line.startswith("        ") or ":" not in line:
            continue
        key, value = line.strip().split(":", 1)
        value = value.strip()
        if value and value != ">-":
            values[key] = value
    return values


def _path_is_covered(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in roots)


def actual_package_files() -> set[str]:
    files: set[str] = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if ".git" in relative.parts:
            continue
        if path.is_file() or path.is_symlink():
            files.add(relative.as_posix())
    return files


def validate_no_symlinks_under(path: Path, label: str, errors: list[str]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        require(False, f"{label} does not exist", errors)
        return
    require(not stat.S_ISLNK(info.st_mode), f"{label} must not be a symlink", errors)
    if stat.S_ISDIR(info.st_mode):
        for child in path.rglob("*"):
            child_info = child.lstat()
            require(
                not stat.S_ISLNK(child_info.st_mode),
                f"{child.relative_to(ROOT).as_posix()} must not be a symlink",
                errors,
            )


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/cline-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(manifest.get("name") == contract.get("product_name"), "manifest name mismatch", errors)
    require(build.get("node_minimum_major") == nddev_cline.MIN_NODE_MAJOR, "Node minimum mismatch", errors)
    require(build.get("node_recommended_major") == nddev_cline.RECOMMENDED_NODE_MAJOR, "Node recommended mismatch", errors)
    require(build.get("nddev_builder_extension_version") == version, "builder version mismatch", errors)
    cli_version = build.get("cline_cli_tested")
    cli_package = build.get("cline_cli_package")
    extension_version = build.get("cline_extension_tested")
    extension_id = build.get("vscode_extension_id")
    require(cli_version == nddev_cline.TESTED_CLI_VERSION, "manager CLI version mismatch", errors)
    require(cli_package == nddev_cline.NPM_PACKAGE, "manager CLI package mismatch", errors)
    require(extension_version == nddev_cline.TESTED_EXTENSION_VERSION, "manager extension version mismatch", errors)
    install_env = build.get("npm_ci_lockfile_install_env")
    require(isinstance(install_env, list), "build npm install env list missing", errors)
    if isinstance(install_env, list):
        require("NPM_CONFIG_IGNORE_SCRIPTS" in install_env, "build env must include NPM_CONFIG_IGNORE_SCRIPTS", errors)
        require("NPM_CONFIG_BIN_LINKS" in install_env, "build env must include NPM_CONFIG_BIN_LINKS", errors)
    npm = baseline.get("npm")
    package_manager = baseline.get("package_manager")
    extension = baseline.get("extension")
    release = baseline.get("release")
    require(isinstance(npm, dict), "baseline npm missing", errors)
    require(isinstance(package_manager, dict), "baseline package_manager missing", errors)
    require(isinstance(extension, dict), "baseline extension missing", errors)
    require(isinstance(release, dict), "baseline release missing", errors)
    if isinstance(npm, dict):
        require(npm.get("version") == cli_version, "baseline npm version mismatch", errors)
        require(npm.get("package") == cli_package, "baseline npm package mismatch", errors)
        require(npm.get("integrity") == build.get("cline_cli_integrity"), "baseline npm integrity mismatch", errors)
        require(npm.get("shasum") == build.get("cline_cli_shasum"), "baseline npm shasum mismatch", errors)
        require(isinstance(npm.get("tarball"), str) and str(cli_version) in npm["tarball"], "baseline npm tarball mismatch", errors)
        optional = npm.get("optional_dependencies")
        require(isinstance(optional, dict), "baseline optional dependencies missing", errors)
        if isinstance(optional, dict):
            require(
                all(value == cli_version for value in optional.values()),
                "baseline optional dependency version mismatch",
                errors,
            )
    if isinstance(package_manager, dict):
        require(package_manager.get("name") == "npm", "package manager must be npm", errors)
        require(
            package_manager.get("official_install_argv") == ["npm", "install", "-g", "cline"],
            "official npm install argv mismatch",
            errors,
        )
        managed_argv = package_manager.get("managed_install_argv")
        require(isinstance(managed_argv, list) and managed_argv[0:2] == ["npm", "ci"], "managed npm install argv mismatch", errors)
        if isinstance(managed_argv, list):
            for flag in NPM_CI_REQUIRED_FLAGS:
                require(flag in managed_argv, f"managed npm install argv missing {flag}", errors)
            for flag in NPM_CI_FORBIDDEN_FLAGS:
                require(flag not in managed_argv, f"managed npm install argv must not contain {flag}", errors)
    if isinstance(extension, dict):
        require(extension.get("id") == extension_id, "baseline extension id mismatch", errors)
        require(extension.get("version") == extension_version, "baseline extension version mismatch", errors)
    if isinstance(release, dict):
        require(release.get("version") == extension_version, "baseline release version mismatch", errors)
        require(release.get("tag") == build.get("cline_extension_release_tag"), "baseline release tag mismatch", errors)
        require(release.get("published_at") == build.get("cline_extension_published_at"), "baseline release published_at mismatch", errors)
        require(
            release.get("assets", {}).get("vscode-vsix", {}).get("sha256")
            == build.get("vscode_extension_vsix_sha256"),
            "baseline VSIX digest mismatch",
            errors,
        )
    runtime = contract.get("runtime_compatibility")
    require(isinstance(runtime, dict), "contract runtime_compatibility missing", errors)
    if isinstance(runtime, dict):
        require(runtime.get("cli_tested_version") == cli_version, "contract CLI version mismatch", errors)
        require(runtime.get("cli_package") == cli_package, "contract CLI package mismatch", errors)
        require(runtime.get("extension_tested_version") == extension_version, "contract extension version mismatch", errors)
        require(runtime.get("vscode_extension_id") == extension_id, "contract extension id mismatch", errors)
        require("windows" in runtime.get("unsupported_platforms", []), "Windows must be unsupported", errors)


def validate_setups_and_profiles(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    require(manifest.get("setup_ids") == SETUP_IDS, "manifest setup ids mismatch", errors)
    require(manifest.get("profile_ids") == PROFILE_IDS, "manifest profile ids mismatch", errors)
    require(manifest.get("default_setup") == "nddev-builder", "default setup mismatch", errors)
    require(manifest.get("default_profile") == "full-auto", "default profile mismatch", errors)
    require(set(manifest.get("managed_files", [])) == expected_managed_files(), "manifest managed files mismatch", errors)
    setup_system = contract.get("setup_system")
    require(isinstance(setup_system, dict), "contract setup_system missing", errors)
    if isinstance(setup_system, dict):
        require(setup_system.get("setup_ids") == SETUP_IDS, "contract setup ids mismatch", errors)
        require(setup_system.get("profile_ids") == PROFILE_IDS, "contract profile ids mismatch", errors)
        require("migrate" in setup_system.get("lifecycle", []), "migrate lifecycle missing", errors)
    setup = read_json("setups/nddev-builder/setup.json")
    settings = read_json("setups/nddev-builder/global-settings.json")
    mcp = read_json("setups/nddev-builder/cline_mcp_settings.json")
    require(setup.get("id") == "nddev-builder", "setup id mismatch", errors)
    require(settings == {}, "global settings must not contain unproven keys", errors)
    require(mcp == {"mcpServers": {}}, "MCP settings must be empty", errors)
    full_auto = read_json("profiles/full-auto/profile.json")
    safe = read_json("profiles/safe/profile.json")
    require(full_auto.get("default") is True, "full-auto must be default", errors)
    require(full_auto.get("sandbox") is False, "full-auto must not use Cline sandbox", errors)
    require(full_auto.get("launch_args") == ["--auto-approve", "true"], "full-auto launch args mismatch", errors)
    require(
        full_auto.get("command_permissions") == {"allow": ["*"], "deny": [], "allowRedirects": True},
        "full-auto command permissions mismatch",
        errors,
    )
    require(safe.get("default") is False, "safe must not be default", errors)
    require(safe.get("sandbox") is True, "safe must use Cline sandbox", errors)
    require(safe.get("launch_args") == ["--plan", "--auto-approve", "false"], "safe launch args mismatch", errors)
    require(
        safe.get("command_permissions") == {"allow": [], "deny": ["*"], "allowRedirects": False},
        "safe command permissions mismatch",
        errors,
    )


def validate_install_lock_assets(errors: list[str]) -> None:
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/cline-baseline.json")
    package_json = read_json("software/cline-cli/package.json")
    package_lock = read_json("software/cline-cli/package-lock.json")
    lock_digest = sha256_file(ROOT / "software/cline-cli/package-lock.json")
    expected_digest = build.get("cline_cli_lockfile_sha256")
    require(lock_digest == expected_digest, "build lockfile digest mismatch", errors)
    require(baseline.get("package_manager", {}).get("lockfile_sha256") == lock_digest, "baseline lockfile digest mismatch", errors)
    require(manifest.get("software_lifecycle", {}).get("lockfile_sha256") == lock_digest, "manifest lockfile digest mismatch", errors)
    require(contract.get("software_install", {}).get("cli", {}).get("lockfile_sha256") == lock_digest, "contract lockfile digest mismatch", errors)
    cli_package = build.get("cline_cli_package")
    cli_version = build.get("cline_cli_tested")
    expected_dependency = {cli_package: cli_version}
    require(package_json.get("dependencies") == expected_dependency, "install package.json root dependency mismatch", errors)
    packages = package_lock.get("packages")
    require(package_lock.get("lockfileVersion") == 3, "package-lock lockfileVersion mismatch", errors)
    require(isinstance(packages, dict), "package-lock packages missing", errors)
    if not isinstance(packages, dict):
        return
    root = packages.get("")
    require(isinstance(root, dict), "package-lock root package missing", errors)
    if isinstance(root, dict):
        require(root.get("dependencies") == expected_dependency, "package-lock root dependency mismatch", errors)
    cline_package = packages.get("node_modules/cline")
    require(isinstance(cline_package, dict), "package-lock cline package missing", errors)
    if isinstance(cline_package, dict):
        require(cline_package.get("version") == cli_version, "package-lock cline package version mismatch", errors)
        require(cline_package.get("bin") == {nddev_cline.COMMAND_NAME: "bin/cline"}, "package-lock cline package wrapper bin mismatch", errors)
        require(
            cline_package.get("optionalDependencies")
            == {package: cli_version for package in nddev_cline.EXPECTED_CLINE_OPTIONAL_PACKAGES},
            "package-lock cline optional dependency map mismatch",
            errors,
        )
    optional_expected = set(baseline.get("npm", {}).get("optional_dependencies", {}))
    optional_seen: set[str] = set()
    for package_path, metadata in packages.items():
        require(isinstance(package_path, str), "package-lock package key must be string", errors)
        require(isinstance(metadata, dict), f"package-lock package {package_path} must be object", errors)
        if not isinstance(package_path, str) or not isinstance(metadata, dict) or package_path == "":
            continue
        package_name = package_path.removeprefix("node_modules/")
        if package_name in optional_expected:
            optional_seen.add(package_name)
            require(metadata.get("version") == cli_version, f"optional package {package_name} version mismatch", errors)
            native_contract = nddev_cline.SUPPORTED_NATIVE_OPTIONAL_PACKAGES.get(package_name)
            if native_contract is not None:
                require(metadata.get("optional") is True, f"optional package {package_name} must be optional", errors)
                require(metadata.get("os") == native_contract["os"], f"optional package {package_name} os selector mismatch", errors)
                require(metadata.get("cpu") == native_contract["cpu"], f"optional package {package_name} cpu selector mismatch", errors)
                require(
                    metadata.get("bin") == {nddev_cline.COMMAND_NAME: native_contract["bin"]},
                    f"optional package {package_name} bin mapping mismatch",
                    errors,
                )
        resolved = metadata.get("resolved")
        if isinstance(resolved, str):
            require(resolved.startswith(nddev_cline.NPM_REGISTRY), f"non-registry resolved URL in lock: {package_path}", errors)
            lowered = resolved.lower()
            require(not lowered.startswith(("git+", "file:", "http://")), f"unsafe resolved URL in lock: {package_path}", errors)
        elif metadata.get("link") is not True:
            require(False, f"resolved URL missing in lock: {package_path}", errors)
        if metadata.get("link") is not True:
            require(isinstance(metadata.get("integrity"), str), f"integrity missing in lock: {package_path}", errors)
    require(optional_seen == optional_expected, "optional Cline platform package set mismatch", errors)
    try:
        nddev_cline.validate_install_lock_contract()
    except nddev_cline.ClineSetupError as exc:
        require(False, f"manager lock validation failed: {exc}", errors)


def validate_builder(errors: list[str]) -> None:
    contract = read_json("config/nddev-contract.json")
    build = read_json("build/version.json")
    package_json = read_json("plugins/nddev-builder/plugins/nddev-builder/package.json")
    builder = contract.get("builder_capability")
    require(isinstance(builder, dict), "contract builder missing", errors)
    if isinstance(builder, dict):
        require(builder.get("version") == build.get("nddev_builder_extension_version"), "builder version mismatch", errors)
        require(builder.get("plugin_distribution") == "native cline.plugins package in home/.cline/plugins", "plugin distribution mismatch", errors)
        require(builder.get("marketplace") is None, "builder marketplace must be null", errors)
    require(package_json.get("name") == "nddev-builder", "builder package name mismatch", errors)
    require(package_json.get("version") == build.get("nddev_builder_extension_version"), "builder package version mismatch", errors)
    require(package_json.get("type") == "module", "builder package must be ESM", errors)
    plugins = package_json.get("cline", {}).get("plugins")
    require(isinstance(plugins, list) and plugins, "builder package missing cline.plugins", errors)
    if isinstance(plugins, list) and plugins:
        require(plugins[0].get("paths") == ["./index.js"], "builder plugin path mismatch", errors)
        require(plugins[0].get("capabilities") == ["tools"], "builder plugin capabilities mismatch", errors)
    for relative in (
        "plugins/nddev-builder/skills/nddev-builder/SKILL.md",
        "plugins/nddev-builder/skills/nddev-builder/references/native-paths.md",
        "plugins/nddev-builder/skills/nddev-builder/references/plugins.md",
        "plugins/nddev-builder/skills/nddev-builder/references/profiles-runtime.md",
        "plugins/nddev-builder/agents/nddev-builder.yaml",
        "plugins/nddev-builder/plugins/nddev-builder/index.js",
    ):
        require((ROOT / relative).is_file(), f"builder native file missing: {relative}", errors)
    index = (ROOT / "plugins/nddev-builder/plugins/nddev-builder/index.js").read_text(encoding="utf-8")
    require("export default plugin" in index, "builder plugin must export an AgentPlugin object", errors)
    require("nddev_builder_read_reference" in index, "builder plugin missing regular-file adapter", errors)


def validate_runtime_contract(errors: list[str]) -> None:
    contract = read_json("config/nddev-contract.json")
    manifest = read_json("build/manifest.json")
    launch = contract.get("runtime_launch")
    software = contract.get("software_install")
    transaction = contract.get("transaction_policy")
    build = read_json("build/version.json")
    baseline = read_json("references/cline-baseline.json")
    npm = baseline.get("npm")
    require(nddev_cline.NPM_PACKAGE_SPEC == f"{build.get('cline_cli_package')}@{build.get('cline_cli_tested')}", "runtime npm package spec mismatch", errors)
    require(nddev_cline.DEFAULT_SETUP_ID == "nddev-builder", "runtime default setup mismatch", errors)
    require(nddev_cline.DEFAULT_PROFILE_ID == "full-auto", "runtime default profile mismatch", errors)
    require(sorted(nddev_cline.BLOCKED_LAUNCH_FLAGS) == sorted(EXPECTED["blocked_launch_flags"]), "blocked launch flags mismatch", errors)
    require(isinstance(launch, dict), "runtime_launch missing", errors)
    require(isinstance(software, dict), "software_install missing", errors)
    require(isinstance(transaction, dict), "transaction_policy missing", errors)
    if isinstance(launch, dict):
        require(launch.get("extension_launch_supported") is False, "extension launch must be unsupported", errors)
        require(launch.get("extension_install_supported") is False, "extension install must be unsupported", errors)
        require(launch.get("token_environment_inheritance") == "stripped", "tokens must be stripped", errors)
        require(launch.get("executable_source") == "validated-target-owned-npm-ci-lockfile-install", "runtime executable source mismatch", errors)
        require(launch.get("blocks_user_managed_flags") == EXPECTED["blocked_launch_flags"], "contract launch flag blocklist mismatch", errors)
        require("legacy" in launch.get("legacy_launch_policy", ""), "legacy launch policy missing", errors)
        require("lock_released_before_child" not in launch, "legacy launch lock release flag must not be present", errors)
        require(launch.get("target_lifecycle_lock") == "held-through-child-completion", "launch lock scope mismatch", errors)
        lock_policy = launch.get("target_lifecycle_lock_policy")
        require(
            isinstance(lock_policy, str)
            and "while target software is running" in lock_policy
            and "restore" in lock_policy
            and "update-cli" in lock_policy,
            "launch lock policy must deny lifecycle mutations while software is running",
            errors,
        )
        if isinstance(lock_policy, str):
            require("fcntl.flock" in lock_policy, "launch lock policy must name fcntl.flock", errors)
            require("O_NOFOLLOW" in lock_policy, "launch lock policy must name O_NOFOLLOW validation", errors)
            require("write-protected verified-path handoff" in lock_policy, "launch handoff truth missing", errors)
            require("not portable fd execution" in lock_policy, "launch policy must not claim portable fd execution", errors)
            require("same-UID chmod" in lock_policy, "launch policy must disclose same-UID chmod boundary", errors)
            require("dedicated target-internal lock directory" in lock_policy, "launch policy must use a dedicated lock directory", errors)
            require("external/bootstrap" in lock_policy, "launch policy must use an external bootstrap lock", errors)
            require("fixed resolved system temp root" in lock_policy, "launch policy must bind bootstrap lock to fixed system temp", errors)
            require("never removed on normal release" in lock_policy, "launch policy must keep bootstrap lock persistent", errors)
            require("external first and internal second" in lock_policy, "launch policy must document lock acquisition order", errors)
            require("internal first and external last" in lock_policy, "launch policy must document lock release order", errors)
            require("renames the internal lock directory" in lock_policy, "launch policy must cover internal lock directory rename", errors)
            require("bootstrap lock root" in lock_policy, "launch policy must disclose bootstrap root same-UID boundary", errors)
            require("target root, HOME, config, TMP, XDG, runtime, and sandbox directories remain writable" in lock_policy, "launch policy must preserve runtime writability", errors)
        require("--data-dir" not in launch.get("full_auto_command", ""), "full-auto command must not include --data-dir", errors)
        require("CLINE_SANDBOX" not in launch.get("full_auto_command", ""), "full-auto command must not set sandbox env", errors)
    if isinstance(software, dict):
        cli = software.get("cli")
        extension = software.get("extension")
        require(isinstance(cli, dict) and cli.get("supported") is True, "CLI install must be supported", errors)
        require(isinstance(extension, dict) and extension.get("supported") is False, "extension install must be unsupported", errors)
        if isinstance(cli, dict):
            require(cli.get("package_manager") == "npm", "CLI package manager must be npm", errors)
            require(cli.get("registry") == nddev_cline.NPM_REGISTRY, "npm registry mismatch", errors)
            require(cli.get("lockfile_sha256") == build.get("cline_cli_lockfile_sha256"), "contract CLI lock digest mismatch", errors)
            require(cli.get("install_argv", [None])[0:2] == ["npm", "ci"], "contract install argv must use npm ci", errors)
            install_argv = cli.get("install_argv")
            require(isinstance(install_argv, list), "contract install argv must be a list", errors)
            if isinstance(install_argv, list):
                for flag in NPM_CI_REQUIRED_FLAGS:
                    require(flag in install_argv, f"contract install argv missing {flag}", errors)
                for flag in NPM_CI_FORBIDDEN_FLAGS:
                    require(flag not in install_argv, f"contract install argv must not contain {flag}", errors)
            lockfile_preflight = cli.get("lockfile_preflight")
            require(
                isinstance(lockfile_preflight, dict)
                and lockfile_preflight.get("cline_package_wrapper_bin") == "bin/cline",
                "contract lockfile preflight must check cline package wrapper bin mapping",
                errors,
            )
            require(cli.get("lifecycle_scripts") == "disabled", "contract lifecycle script policy mismatch", errors)
            require(cli.get("bin_links") == "disabled", "contract bin-links policy mismatch", errors)
            environment_policy = cli.get("environment_policy")
            require(isinstance(environment_policy, dict), "contract environment policy missing", errors)
            if isinstance(environment_policy, dict):
                required_env = environment_policy.get("required")
                require(isinstance(required_env, list), "contract required env list missing", errors)
                if isinstance(required_env, list):
                    require("NPM_CONFIG_IGNORE_SCRIPTS" in required_env, "contract env must require NPM_CONFIG_IGNORE_SCRIPTS", errors)
                    require("NPM_CONFIG_BIN_LINKS" in required_env, "contract env must require NPM_CONFIG_BIN_LINKS", errors)
            if isinstance(npm, dict):
                registry_metadata = cli.get("registry_metadata", {})
                require(registry_metadata.get("integrity") == npm.get("integrity"), "registry integrity mismatch", errors)
                require(registry_metadata.get("shasum") == npm.get("shasum"), "registry shasum mismatch", errors)
                require(registry_metadata.get("tarball") == npm.get("tarball"), "registry tarball mismatch", errors)
            require(
                cli.get("node_preflight")
                == {
                    "minimum_major": nddev_cline.MIN_NODE_MAJOR,
                    "recommended_major": nddev_cline.RECOMMENDED_NODE_MAJOR,
                },
                "Node preflight mismatch",
                errors,
            )
            require(cli.get("layout", {}).get("package_wrapper") == str(nddev_cline.PACKAGE_WRAPPER_RELATIVE), "package wrapper path mismatch", errors)
            require(cli.get("version_probe", {}).get("timeout_seconds") == nddev_cline.VERSION_PROBE_TIMEOUT_SECONDS, "version probe timeout mismatch", errors)
    lifecycle = manifest.get("software_lifecycle")
    require(isinstance(lifecycle, dict), "manifest software_lifecycle missing", errors)
    if isinstance(lifecycle, dict):
        require(lifecycle.get("install_argv", [None])[0:2] == ["npm", "ci"], "manifest install argv must use npm ci", errors)
        require(lifecycle.get("lockfile_sha256") == build.get("cline_cli_lockfile_sha256"), "manifest lock digest mismatch", errors)
        install_argv = lifecycle.get("install_argv")
        require(isinstance(install_argv, list), "manifest install argv must be a list", errors)
        if isinstance(install_argv, list):
            for flag in NPM_CI_REQUIRED_FLAGS:
                require(flag in install_argv, f"manifest install argv missing {flag}", errors)
            for flag in NPM_CI_FORBIDDEN_FLAGS:
                require(flag not in install_argv, f"manifest install argv must not contain {flag}", errors)
        require(lifecycle.get("lifecycle_scripts") == "disabled", "manifest lifecycle script policy mismatch", errors)
        require(lifecycle.get("bin_links") == "disabled", "manifest bin-links policy mismatch", errors)
        expected_node_preflight = (
            f"Node.js {nddev_cline.MIN_NODE_MAJOR}+ required; "
            f"{nddev_cline.RECOMMENDED_NODE_MAJOR} recommended"
        )
        require(lifecycle.get("node_preflight") == expected_node_preflight, "manifest Node preflight mismatch", errors)
        handoff = lifecycle.get("launch_handoff_policy")
        require(
            isinstance(handoff, str)
            and "path-based spawn" in handoff
            and "external/bootstrap" in handoff
            and "fixed system temp" in handoff
            and "acquisition external first/internal second" in handoff
            and "mutable target, HOME, config, TMP, XDG, runtime, and sandbox directories stay writable" in handoff,
            "manifest launch handoff policy mismatch",
            errors,
        )
        bounds = lifecycle.get("bounds")
        require(isinstance(bounds, dict), "manifest software bounds missing", errors)
        if isinstance(bounds, dict):
            require(bounds.get("max_tree_paths") == nddev_cline.SOFTWARE_TREE_MAX_PATHS, "software path bound mismatch", errors)
            require(bounds.get("max_tree_bytes") == nddev_cline.SOFTWARE_TREE_MAX_BYTES, "software byte bound mismatch", errors)


def validate_bootstrap_lock_contract(errors: list[str]) -> None:
    source = (ROOT / "cli-tools/nddev_cline.py").read_text(encoding="utf-8")
    expected_root = Path("/private/tmp").resolve() if sys.platform.startswith("darwin") else Path("/tmp").resolve()
    observed_root = nddev_cline.fixed_system_temp_root()
    require(observed_root == expected_root, "bootstrap lock root must use the fixed system temp root", errors)
    try:
        info = observed_root.lstat()
    except FileNotFoundError:
        require(False, "bootstrap fixed system temp root is missing", errors)
    else:
        require(stat.S_ISDIR(info.st_mode), "bootstrap fixed system temp root must be a directory", errors)
        require(not stat.S_ISLNK(info.st_mode), "bootstrap fixed system temp root must not be a symlink", errors)
        require(bool(stat.S_IMODE(info.st_mode) & stat.S_ISVTX), "bootstrap fixed system temp root must be sticky", errors)
    require("gettempdir(" not in source, "bootstrap lock root must not use tempfile.gettempdir", errors)
    for name in FORBIDDEN_BOOTSTRAP_OVERRIDE_NAMES:
        require(name not in source, f"public bootstrap override must not exist: {name}", errors)


def lock_identity_payload(canonical_target: Path) -> dict[str, Any]:
    path = nddev_cline.bootstrap_lock_path(canonical_target)
    info = path.lstat()
    return {
        "path": str(path),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
    }


def validate_bootstrap_lock_handover(errors: list[str]) -> None:
    require(hasattr(os, "fork"), "bootstrap lock handover smoke requires POSIX fork", errors)
    if not hasattr(os, "fork"):
        return
    with tempfile.TemporaryDirectory(prefix="nddev-cline-lock-handover-") as raw:
        root = Path(raw)
        root.chmod(0o700)
        target = root / "target"
        with nddev_cline.bootstrap_lifecycle_lock(target) as canonical_target:
            lock_path = nddev_cline.bootstrap_lock_path(canonical_target)
            initial_identity = path_identity(lock_path)
        a_ready_r, a_ready_w = os.pipe()
        a_release_r, a_release_w = os.pipe()
        b_blocked_r, b_blocked_w = os.pipe()
        b_ready_r, b_ready_w = os.pipe()
        b_release_r, b_release_w = os.pipe()
        c_ready_r, c_ready_w = os.pipe()

        pid_a = os.fork()
        if pid_a == 0:
            try:
                os.close(a_ready_r)
                os.close(a_release_w)
                with nddev_cline.bootstrap_lifecycle_lock(target) as canonical:
                    write_pipe_json(a_ready_w, {"ok": True, **lock_identity_payload(canonical)})
                    os.read(a_release_r, 1)
                os._exit(0)
            except BaseException as exc:
                with contextlib.suppress(OSError):
                    write_pipe_json(a_ready_w, {"ok": False, "error": str(exc)})
                os._exit(1)
        os.close(a_ready_w)
        os.close(a_release_r)
        a_report = read_pipe_json(a_ready_r, "bootstrap actor A", errors)
        os.close(a_ready_r)
        require(a_report.get("ok") is True, "bootstrap actor A did not acquire lock", errors)
        require(a_report.get("mode") == 0o600, "bootstrap actor A lock mode mismatch", errors)
        require(a_report.get("nlink") == 1, "bootstrap actor A lock link count mismatch", errors)

        pid_b = os.fork()
        if pid_b == 0:
            try:
                os.close(b_blocked_r)
                os.close(b_ready_r)
                os.close(b_release_w)
                reported_blocked = False
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        with nddev_cline.bootstrap_lifecycle_lock(target) as canonical:
                            write_pipe_json(
                                b_ready_w,
                                {
                                    "ok": True,
                                    "blocked_first": reported_blocked,
                                    **lock_identity_payload(canonical),
                                },
                            )
                            os.read(b_release_r, 1)
                            os._exit(0)
                    except nddev_cline.ClineSetupError as exc:
                        if "locked" not in str(exc):
                            raise
                        if not reported_blocked:
                            write_pipe_json(b_blocked_w, {"ok": True, "error": str(exc)})
                            reported_blocked = True
                        time.sleep(0.025)
                write_pipe_json(b_ready_w, {"ok": False, "error": "timed out waiting for handover"})
                os._exit(1)
            except BaseException as exc:
                with contextlib.suppress(OSError):
                    write_pipe_json(b_ready_w, {"ok": False, "error": str(exc)})
                os._exit(1)
        os.close(b_blocked_w)
        os.close(b_ready_w)
        os.close(b_release_r)
        b_blocked = read_pipe_json(b_blocked_r, "bootstrap actor B blocked attempt", errors)
        os.close(b_blocked_r)
        require(b_blocked.get("ok") is True and "locked" in str(b_blocked.get("error")), "bootstrap actor B did not observe A lock", errors)
        os.write(a_release_w, b"1")
        os.close(a_release_w)
        wait_child_success(pid_a, "bootstrap actor A", errors)
        b_report = read_pipe_json(b_ready_r, "bootstrap actor B", errors)
        os.close(b_ready_r)
        require(b_report.get("ok") is True, "bootstrap actor B did not acquire after handover", errors)
        require(b_report.get("blocked_first") is True, "bootstrap actor B did not retry after a locked handover", errors)

        pid_c = os.fork()
        if pid_c == 0:
            try:
                os.close(c_ready_r)
                try:
                    with nddev_cline.bootstrap_lifecycle_lock(target) as canonical:
                        write_pipe_json(c_ready_w, {"ok": False, "acquired": True, **lock_identity_payload(canonical)})
                except nddev_cline.ClineSetupError as exc:
                    write_pipe_json(c_ready_w, {"ok": True, "error": str(exc)})
                os._exit(0)
            except BaseException as exc:
                with contextlib.suppress(OSError):
                    write_pipe_json(c_ready_w, {"ok": False, "error": str(exc)})
                os._exit(1)
        os.close(c_ready_w)
        c_report = read_pipe_json(c_ready_r, "bootstrap actor C", errors)
        os.close(c_ready_r)
        require(c_report.get("ok") is True and "locked" in str(c_report.get("error")), "bootstrap actor C did not observe B lock", errors)
        os.write(b_release_w, b"1")
        os.close(b_release_w)
        wait_child_success(pid_b, "bootstrap actor B", errors)
        wait_child_success(pid_c, "bootstrap actor C", errors)
        final_identity = path_identity(lock_path)
        require(initial_identity is not None, "bootstrap lock initial identity missing", errors)
        if initial_identity is not None:
            require(
                tuple(a_report.get(key) for key in ("dev", "ino")) == initial_identity[:2],
                "bootstrap actor A used a different lock inode",
                errors,
            )
            require(
                tuple(b_report.get(key) for key in ("dev", "ino")) == initial_identity[:2],
                "bootstrap actor B used a different lock inode after handover",
                errors,
            )
            require(final_identity is not None and final_identity[:2] == initial_identity[:2], "bootstrap lock inode was replaced", errors)


def validate_bootstrap_lock_smokes(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-cline-bootstrap-smoke-") as raw:
        root = Path(raw)
        root.chmod(0o700)
        target = root / "target"
        with nddev_cline.bootstrap_lifecycle_lock(target) as canonical_target:
            lock_path = nddev_cline.bootstrap_lock_path(canonical_target)
            product_root = lock_path.parent
            require(product_root.parent == nddev_cline.fixed_system_temp_root(), "bootstrap lock escaped injected fixed root", errors)
            locked_identity = path_identity(lock_path)
        after_identity = path_identity(lock_path)
        require(locked_identity is not None and after_identity == locked_identity, "bootstrap lock file did not persist after release", errors)
        if lock_path.exists():
            content = lock_path.read_bytes()
            binding = json.loads(content.decode("utf-8"))
            require(binding == nddev_cline.bootstrap_lock_binding(canonical_target), "bootstrap lock binding mismatch", errors)
        nddev_cline.mutate_setup(target, "nddev-builder", "full-auto", "install")
        before_remove_identity = path_identity(lock_path)
        nddev_cline.remove_setup(target)
        require(path_identity(lock_path) == before_remove_identity, "setup remove removed or replaced bootstrap lock", errors)

        bad_target = root / "bad-target"
        with nddev_cline.bootstrap_lifecycle_lock(bad_target) as bad_canonical:
            bad_lock = nddev_cline.bootstrap_lock_path(bad_canonical)
        wrong_binding = nddev_cline.bootstrap_lock_binding(root / "other-target")
        bad_lock.write_bytes(nddev_cline.canonical_json(wrong_binding))
        bad_lock.chmod(0o600)
        observed_error = None
        try:
            with nddev_cline.bootstrap_lifecycle_lock(bad_target):
                pass
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        require(
            isinstance(observed_error, str) and "different canonical target" in observed_error,
            "bootstrap binding mismatch was not rejected",
            errors,
        )

        symlink_target = root / "symlink-target"
        symlink_canonical = nddev_cline.canonical_target_for_bootstrap_lock(symlink_target)
        symlink_lock = nddev_cline.bootstrap_lock_path(symlink_canonical)
        external = root / "external-lock"
        external.write_text("sentinel\n", encoding="utf-8")
        external.chmod(0o600)
        if symlink_lock.exists() or symlink_lock.is_symlink():
            symlink_lock.unlink()
        os.symlink(external, symlink_lock)
        observed_error = None
        try:
            with nddev_cline.bootstrap_lifecycle_lock(symlink_target):
                pass
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        require(
            isinstance(observed_error, str) and "symlink" in observed_error,
            "bootstrap symlink lock was not rejected",
            errors,
        )


def install_validator_stub_cline(target: Path) -> None:
    package_wrapper = target / nddev_cline.PACKAGE_WRAPPER_RELATIVE
    package_wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    package_wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then printf '%s\\n' "
        f"{nddev_cline.TESTED_CLI_VERSION!r}; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    package_wrapper.chmod(0o700)
    visible = target / "bin" / nddev_cline.COMMAND_NAME
    visible.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    visible.write_text(
        "#!/bin/sh\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'exec "$SCRIPT_DIR"/../software/cline-cli/install/project/node_modules/cline/bin/cline "$@"\n',
        encoding="utf-8",
    )
    visible.chmod(0o700)
    for directory in sorted(
        {path for path in (target / "bin", package_wrapper.parent, *package_wrapper.parents) if path.is_relative_to(target)},
        key=lambda item: len(item.parts),
    ):
        if directory.exists() and directory.is_dir():
            directory.chmod(0o700)
    manifest = target / nddev_cline.SOFTWARE_MANIFEST_RELATIVE
    manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest.write_bytes(nddev_cline.canonical_json(nddev_cline.build_software_manifest(target.resolve())))
    manifest.chmod(0o600)


def validate_launch_profiles(errors: list[str]) -> None:
    captures: list[dict[str, Any]] = []

    def fake_run(argv: list[str], *, cwd: str, env: dict[str, str], check: bool, timeout: None) -> subprocess.CompletedProcess[str]:
        del cwd, check, timeout
        executable = Path(argv[0])
        launch_target = executable.parent.parent
        concurrent_errors: dict[str, str | None] = {}
        lock_unlink_error: str | None = None
        replace_error: str | None = None
        internal_lock_rename_error: str | None = None
        internal_lock_restore_error: str | None = None
        lock_file = nddev_cline.lock_path(launch_target)
        lock_directory = nddev_cline.lock_directory_path(launch_target)
        lock_file_mode = stat.S_IMODE(lock_file.lstat().st_mode) if lock_file.exists() else None
        lock_directory_mode = stat.S_IMODE(lock_directory.lstat().st_mode) if lock_directory.exists() else None
        target_mode_during_child = stat.S_IMODE(launch_target.lstat().st_mode)
        bin_mode_during_child = stat.S_IMODE(executable.parent.lstat().st_mode)
        executable_mode_during_child = stat.S_IMODE(executable.lstat().st_mode)
        runtime_write_errors: list[str] = []
        runtime_writes = [
            Path(env["HOME"]) / ".cline" / "session-state.json",
            Path(env["TMPDIR"]) / "cline.tmp",
            Path(env["XDG_CONFIG_HOME"]) / "cline-config-state.json",
            Path(env["XDG_CACHE_HOME"]) / "cline-cache-state.json",
            Path(env["XDG_STATE_HOME"]) / "cline-state.json",
        ]
        if "--config" in argv:
            config_index = argv.index("--config")
            runtime_writes.append(Path(argv[config_index + 1]) / "runtime-write.json")
        sandbox = env.get("CLINE_SANDBOX_DATA_DIR")
        if sandbox:
            runtime_writes.append(Path(sandbox) / "sandbox-write.json")
        for path in runtime_writes:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text("runtime-ok\n", encoding="utf-8")
            except OSError as exc:
                runtime_write_errors.append(f"{path}: {exc.__class__.__name__}")
        try:
            lock_file.unlink()
        except OSError as exc:
            lock_unlink_error = exc.__class__.__name__
        replacement = launch_target.parent / "replacement-cline"
        replacement.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        replacement.chmod(0o700)
        try:
            os.replace(replacement, executable)
        except OSError as exc:
            replace_error = exc.__class__.__name__
        finally:
            replacement.unlink(missing_ok=True)
        renamed_lock_directory = launch_target / ".renamed-nddev-cline-lock"
        try:
            os.replace(lock_directory, renamed_lock_directory)
        except OSError as exc:
            internal_lock_rename_error = exc.__class__.__name__
        for operation, callback in (
            ("switch", lambda: nddev_cline.mutate_setup(launch_target, "nddev-builder", "safe", "switch")),
            ("remove", lambda: nddev_cline.remove_setup(launch_target)),
            ("install", lambda: nddev_cline.mutate_setup(launch_target, "nddev-builder", "full-auto", "install")),
        ):
            try:
                callback()
            except nddev_cline.ClineSetupError as exc:
                concurrent_errors[operation] = str(exc)
            else:
                concurrent_errors[operation] = None
        if renamed_lock_directory.exists() or renamed_lock_directory.is_symlink():
            try:
                os.replace(renamed_lock_directory, lock_directory)
            except OSError as exc:
                internal_lock_restore_error = exc.__class__.__name__
        captures.append(
            {
                "argv": argv,
                "env": env,
                "lock_held": lock_file.is_file(),
                "lock_file_mode": lock_file_mode,
                "lock_directory_mode": lock_directory_mode,
                "lock_unlink_error": lock_unlink_error,
                "lock_survived_unlink_attempt": lock_file.is_file(),
                "replace_error": replace_error,
                "executable_survived_replace_attempt": executable.is_file(),
                "internal_lock_rename_error": internal_lock_rename_error,
                "internal_lock_restore_error": internal_lock_restore_error,
                "concurrent_errors": concurrent_errors,
                "runtime_write_errors": runtime_write_errors,
                "runtime_write_paths": [str(path) for path in runtime_writes],
                "target_mode_during_child": target_mode_during_child,
                "bin_mode_during_child": bin_mode_during_child,
                "executable_mode_during_child": executable_mode_during_child,
            }
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    original_run = subprocess.run
    try:
        subprocess.run = fake_run  # type: ignore[assignment]
        with tempfile.TemporaryDirectory(prefix="nddev-cline-launch-") as raw:
            root = Path(raw)
            target = root / "target"
            target.parent.chmod(0o700)
            nddev_cline.mutate_setup(target, "nddev-builder", "full-auto", "install")
            canonical_target = target.resolve()
            install_validator_stub_cline(canonical_target)
            rc = nddev_cline.launch_cline(canonical_target, ["hello"])
            require(rc == 0, "full-auto fake launch failed", errors)
            full_auto = captures.pop()
            argv = full_auto["argv"][1:]
            env = full_auto["env"]
            require("--auto-approve" in argv, "full-auto missing auto-approve", errors)
            require("--config" in argv and str(canonical_target / nddev_cline.CLINE_CONFIG_RELATIVE) in argv, "full-auto config path mismatch", errors)
            require("--hooks-dir" in argv and str(canonical_target / nddev_cline.CLINE_HOOKS_RELATIVE) in argv, "full-auto hooks path mismatch", errors)
            require("--data-dir" not in argv, "full-auto must not pass --data-dir", errors)
            require("--yolo" not in argv, "full-auto must not pass --yolo", errors)
            require(full_auto.get("lock_held") is True, "launch lock was not held through child execution", errors)
            require(full_auto.get("lock_file_mode") == 0o600, "launch lock file was not owner-only", errors)
            require(full_auto.get("lock_directory_mode") == 0o500, "lock directory was not read/execute-only during child execution", errors)
            require(full_auto.get("target_mode_during_child") == 0o700, "target root must remain writable during child execution", errors)
            require(full_auto.get("bin_mode_during_child") == 0o500, "executable parent was not read/execute-only during child execution", errors)
            require(full_auto.get("executable_mode_during_child") == 0o500, "executable was not read/execute-only during child execution", errors)
            require(full_auto.get("lock_unlink_error") is not None, "child lock unlink attempt was not denied", errors)
            require(full_auto.get("lock_survived_unlink_attempt") is True, "child removed the lifecycle lock file", errors)
            require(full_auto.get("replace_error") is not None, "child executable replace attempt was not denied", errors)
            require(full_auto.get("executable_survived_replace_attempt") is True, "child replaced the executable", errors)
            require(full_auto.get("runtime_write_errors") == [], "full-auto runtime writes were blocked", errors)
            require(full_auto.get("internal_lock_rename_error") is None, "child could not rename internal lock directory", errors)
            require(full_auto.get("internal_lock_restore_error") is None, "child did not restore internal lock directory", errors)
            concurrent = full_auto.get("concurrent_errors")
            require(isinstance(concurrent, dict), "full-auto concurrent lifecycle results missing", errors)
            if isinstance(concurrent, dict):
                for operation in ("switch", "remove", "install"):
                    message = concurrent.get(operation)
                    require(
                        isinstance(message, str) and "locked" in message,
                        f"manager {operation} during renamed internal lock was not blocked by external lock",
                        errors,
                    )
            require("CLINE_DATA_DIR" not in env, "full-auto must not set CLINE_DATA_DIR", errors)
            require("CLINE_SANDBOX" not in env, "full-auto must not set CLINE_SANDBOX", errors)
            require(env.get("HOME") == str(canonical_target / "home"), "full-auto HOME mismatch", errors)
            require(env.get("PATH") == nddev_cline.DETERMINISTIC_PATH, "full-auto PATH must be deterministic", errors)
            require(
                json.loads(env.get("CLINE_COMMAND_PERMISSIONS", "{}")) == {"allow": ["*"], "deny": [], "allowRedirects": True},
                "full-auto command permissions env mismatch",
                errors,
            )
            nddev_cline.mutate_setup(target, "nddev-builder", "safe", "switch")
            rc = nddev_cline.launch_cline(canonical_target, ["hello"])
            require(rc == 0, "safe fake launch failed", errors)
            safe = captures.pop()
            argv = safe["argv"][1:]
            env = safe["env"]
            require("--plan" in argv, "safe missing --plan", errors)
            require("--data-dir" in argv and str(canonical_target / nddev_cline.CLINE_SANDBOX_RELATIVE) in argv, "safe data-dir mismatch", errors)
            require(safe.get("lock_held") is True, "safe launch lock was not held through child execution", errors)
            require(safe.get("lock_directory_mode") == 0o500, "safe lock directory was not protected during child execution", errors)
            require(safe.get("target_mode_during_child") == 0o700, "safe target root must remain writable during child execution", errors)
            require(safe.get("lock_unlink_error") is not None, "safe child lock unlink attempt was not denied", errors)
            require(safe.get("replace_error") is not None, "safe child executable replace attempt was not denied", errors)
            require(safe.get("runtime_write_errors") == [], "safe runtime writes were blocked", errors)
            require(safe.get("internal_lock_rename_error") is None, "safe child could not rename internal lock directory", errors)
            require(safe.get("internal_lock_restore_error") is None, "safe child did not restore internal lock directory", errors)
            concurrent = safe.get("concurrent_errors")
            require(isinstance(concurrent, dict), "safe concurrent lifecycle results missing", errors)
            if isinstance(concurrent, dict):
                for operation in ("switch", "remove", "install"):
                    message = concurrent.get(operation)
                    require(
                        isinstance(message, str) and "locked" in message,
                        f"safe manager {operation} during renamed internal lock was not blocked by external lock",
                        errors,
                    )
            require(env.get("CLINE_SANDBOX") == "1", "safe must set CLINE_SANDBOX=1", errors)
            require("CLINE_DATA_DIR" not in env, "safe should let --data-dir drive data dir", errors)
            require(env.get("PATH") == nddev_cline.DETERMINISTIC_PATH, "safe PATH must be deterministic", errors)
    finally:
        subprocess.run = original_run  # type: ignore[assignment]


def validate_npm_stage_and_timeout(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-cline-public-regression-") as raw_root:
        root = Path(raw_root)
        stage = root / "stage"
        live = root / "live"
        stage.mkdir(mode=0o700)
        live.mkdir(mode=0o700)
        env, userconfig, globalconfig, project_dir = nddev_cline.install_stage_environment(stage, live)
        require(project_dir == live / nddev_cline.INSTALL_PROJECT_RELATIVE, "npm stage project path mismatch", errors)
        require(env.get("PATH") == nddev_cline.DETERMINISTIC_PATH, "npm stage PATH must be deterministic", errors)
        require(env.get("NPM_CONFIG_PREFIX") is None, "npm stage must not set a global prefix", errors)
        require(env.get("NPM_CONFIG_CACHE") == str(stage / "cache"), "npm cache env mismatch", errors)
        require(env.get("NPM_CONFIG_USERCONFIG") == str(userconfig), "npm userconfig env mismatch", errors)
        require(env.get("NPM_CONFIG_GLOBALCONFIG") == str(globalconfig), "npm globalconfig env mismatch", errors)
        require(env.get("NPM_CONFIG_IGNORE_SCRIPTS") == "true", "npm stage must disable lifecycle scripts via env", errors)
        require(env.get("NPM_CONFIG_BIN_LINKS") == "false", "npm stage must disable npm bin links via env", errors)
        require("CLINE_DATA_DIR" not in env and "CLINE_SANDBOX" not in env, "npm stage must not set Cline runtime env", errors)
        npmrc = userconfig.read_text(encoding="utf-8")
        npmrc_settings = dict(
            line.split("=", 1)
            for line in npmrc.splitlines()
            if line and not line.lstrip().startswith("#") and "=" in line
        )
        require(f"registry={nddev_cline.NPM_REGISTRY}" in npmrc, "npmrc registry mismatch", errors)
        require("prefix=" not in npmrc, "npmrc must not set a global prefix", errors)
        require(npmrc_settings.get("ignore-scripts") == "true", "npmrc must disable lifecycle scripts", errors)
        require(npmrc_settings.get("bin-links") == "false", "npmrc must disable npm bin links", errors)
        require("ignore-scripts=false" not in npmrc, "npmrc must not enable lifecycle scripts", errors)
        require("bin-links=true" not in npmrc, "npmrc must not enable npm bin links", errors)
        require("auth" not in npmrc.lower() and "token" not in npmrc.lower(), "npmrc must not contain auth material", errors)
        package_wrapper = live / nddev_cline.PACKAGE_WRAPPER_RELATIVE
        package_wrapper.parent.mkdir(mode=0o700, parents=True)
        package_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        package_wrapper.chmod(0o755)
        require(not (live / nddev_cline.INSTALL_PROJECT_RELATIVE / "node_modules" / ".bin" / nddev_cline.COMMAND_NAME).exists(), "test setup must not create npm .bin link", errors)
        nddev_cline.normalize_stage_executable(live)
        visible_wrapper = live / "bin" / nddev_cline.COMMAND_NAME
        require(visible_wrapper.is_file(), "visible Cline wrapper was not created", errors)
        visible_text = visible_wrapper.read_text(encoding="utf-8")
        require("node_modules/.bin" not in visible_text, "visible wrapper must not rely on npm .bin links", errors)
        require(str(Path("..") / nddev_cline.PACKAGE_WRAPPER_RELATIVE) in visible_text, "visible wrapper must invoke package wrapper", errors)
        captured_npm: dict[str, Any] = {}

        def fake_trusted_executable(name: str) -> str:
            return f"/trusted/{name}"

        def fake_bounded_process(
            argv: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            label: str,
            timeout: int = nddev_cline.PROCESS_TIMEOUT_SECONDS,
        ) -> subprocess.CompletedProcess[str]:
            del timeout
            if label == "Node.js preflight":
                return subprocess.CompletedProcess(argv, 0, "v22.0.0\n", "")
            if label == "npm Cline CLI locked install":
                captured_npm["argv"] = argv
                captured_npm["cwd"] = str(cwd)
                captured_npm["env"] = env
                generated_wrapper = live_install / nddev_cline.PACKAGE_WRAPPER_RELATIVE
                generated_wrapper.parent.mkdir(mode=0o700, parents=True)
                generated_wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                generated_wrapper.chmod(0o755)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if label == "Cline CLI version probe":
                return subprocess.CompletedProcess(argv, 0, f"{nddev_cline.TESTED_CLI_VERSION}\n", "")
            raise AssertionError(f"unexpected bounded process label: {label}")

        stage_install = root / "stage-install"
        live_install = root / "live-install"
        stage_install.mkdir(mode=0o700)
        live_install.mkdir(mode=0o700)
        original_trusted_executable = nddev_cline.trusted_executable
        original_bounded_process = nddev_cline.run_bounded_process
        try:
            nddev_cline.trusted_executable = fake_trusted_executable  # type: ignore[assignment]
            nddev_cline.run_bounded_process = fake_bounded_process  # type: ignore[assignment]
            nddev_cline.run_npm_install(stage_install, live_install)
        finally:
            nddev_cline.trusted_executable = original_trusted_executable  # type: ignore[assignment]
            nddev_cline.run_bounded_process = original_bounded_process  # type: ignore[assignment]
        npm_argv = captured_npm.get("argv")
        require(isinstance(npm_argv, list), "npm install argv was not captured", errors)
        if isinstance(npm_argv, list):
            for flag in NPM_CI_REQUIRED_FLAGS:
                require(flag in npm_argv, f"manager npm install argv missing {flag}", errors)
            for flag in NPM_CI_FORBIDDEN_FLAGS:
                require(flag not in npm_argv, f"manager npm install argv must not contain {flag}", errors)
        require(captured_npm.get("cwd") == str(live_install / nddev_cline.INSTALL_PROJECT_RELATIVE), "manager npm install cwd mismatch", errors)
        npm_env = captured_npm.get("env")
        require(isinstance(npm_env, dict), "npm install env was not captured", errors)
        if isinstance(npm_env, dict):
            require(npm_env.get("NPM_CONFIG_IGNORE_SCRIPTS") == "true", "manager npm env must disable lifecycle scripts", errors)
            require(npm_env.get("NPM_CONFIG_BIN_LINKS") == "false", "manager npm env must disable bin links", errors)
        require(nddev_cline.parse_node_major("v20.19.0") == 20, "Node parser mismatch", errors)
        target = root / "target"
        target.mkdir(mode=0o700)
        target_bin = target / "bin"
        target_bin.mkdir(mode=0o700)
        sentinel = target_bin / "cline"
        sentinel.write_bytes(b"preexisting-partial-runtime\n")
        sentinel.chmod(0o700)
        sentinel_before = nddev_cline.sha256_bytes(sentinel.read_bytes())
        timeout_message = "Cline CLI install timed out before swap"

        def timed_out_stage(stage_root: Path, live_stage: Path) -> dict[str, Any]:
            del stage_root, live_stage
            raise nddev_cline.ClineSetupError(timeout_message)

        original_npm_install = nddev_cline.run_npm_install
        nddev_cline.run_npm_install = timed_out_stage  # type: ignore[assignment]
        observed_error: str | None = None
        try:
            nddev_cline.install_or_update_cli(target, operation="update-cli")
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        finally:
            nddev_cline.run_npm_install = original_npm_install  # type: ignore[assignment]
        require(observed_error == timeout_message, "npm timeout did not fail closed", errors)
        require(sentinel.is_file(), "npm timeout removed the existing runtime", errors)
        if sentinel.is_file():
            require(nddev_cline.sha256_bytes(sentinel.read_bytes()) == sentinel_before, "npm timeout changed the existing runtime", errors)
        lock_file = nddev_cline.lock_path(target)
        require(lock_file.is_file(), "persistent target lock file missing after timeout", errors)
        if lock_file.is_file():
            require(stat.S_IMODE(lock_file.lstat().st_mode) == 0o600, "persistent target lock file mode mismatch", errors)
        lock_directory = nddev_cline.lock_directory_path(target)
        require(lock_directory.is_dir(), "persistent target lock directory missing after timeout", errors)
        if lock_directory.is_dir():
            require(stat.S_IMODE(lock_directory.lstat().st_mode) == 0o700, "persistent target lock directory mode mismatch after timeout", errors)
        require(not list(root.glob(".target.nddev-cline-cli-stage.*")), "npm timeout left staging directory behind", errors)


def validate_security_smokes(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-cline-security-") as raw_root:
        root = Path(raw_root)
        root.chmod(0o700)
        world_target = root / "world-target"
        world_target.mkdir(mode=0o777)
        world_target.chmod(0o777)
        observed_error: str | None = None
        try:
            nddev_cline.mutate_setup(world_target, "nddev-builder", "full-auto", "install")
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        require(observed_error is not None and "mode 0700" in observed_error, "0777 target was not rejected", errors)

        locked_target = root / "locked-target"
        locked_target.mkdir(mode=0o700)
        external_lock = root / "external-lock"
        external_lock.write_text("sentinel\n", encoding="utf-8")
        external_lock.chmod(0o600)
        lock_directory = nddev_cline.lock_directory_path(locked_target)
        lock_directory.mkdir(mode=0o700)
        os.symlink(external_lock, nddev_cline.lock_path(locked_target))
        observed_error = None
        try:
            nddev_cline.mutate_setup(locked_target, "nddev-builder", "full-auto", "install")
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        require(observed_error is not None and "lock" in observed_error, "symlink lock path was not rejected", errors)
        require(external_lock.read_text(encoding="utf-8") == "sentinel\n", "external lock target was changed", errors)

        protected_target = root / "crash-protected-target"
        nddev_cline.mutate_setup(protected_target, "nddev-builder", "full-auto", "install")
        protected_canonical = protected_target.resolve()
        lock_directory = nddev_cline.lock_directory_path(protected_canonical)
        lock_directory.chmod(0o500)
        nddev_cline.mutate_setup(protected_canonical, "nddev-builder", "safe", "switch")
        recovered = nddev_cline.inspect_target(protected_canonical)
        require(recovered.get("profile_id") == "safe", "target with stale protected lock directory did not recover", errors)
        require(stat.S_IMODE(protected_canonical.lstat().st_mode) == 0o700, "target root mode changed during lock-directory recovery", errors)
        require(stat.S_IMODE(lock_directory.lstat().st_mode) == 0o700, "stale protected lock directory mode was not restored", errors)

        backup_target = root / "backup-target"
        nddev_cline.mutate_setup(backup_target, "nddev-builder", "full-auto", "install")
        external_backup = root / "external-backup"
        external_backup.mkdir(mode=0o700)
        os.symlink(external_backup, nddev_cline.backup_pool(backup_target.resolve()))
        observed_error = None
        try:
            nddev_cline.mutate_setup(backup_target, "nddev-builder", "safe", "switch")
        except nddev_cline.ClineSetupError as exc:
            observed_error = str(exc)
        require(observed_error is not None and "backup pool" in observed_error, "symlink backup pool was not rejected", errors)
        require(external_backup.is_dir(), "external backup target was removed", errors)

        sibling_target = root / "sibling-target"
        nddev_cline.mutate_setup(sibling_target, "nddev-builder", "full-auto", "install")
        sibling_pool = nddev_cline.legacy_backup_pool(sibling_target.resolve())
        sibling_pool.mkdir(mode=0o700)
        sibling_marker = sibling_pool / "external-marker"
        sibling_marker.write_text("external\n", encoding="utf-8")
        sibling_marker.chmod(0o600)
        nddev_cline.mutate_setup(sibling_target, "nddev-builder", "safe", "switch")
        require(sibling_marker.read_text(encoding="utf-8") == "external\n", "external sibling marker was changed", errors)
        require(nddev_cline.backup_pool(sibling_target.resolve()).is_dir(), "target-internal backup pool missing", errors)

        for name, relative, profile_id in (
            ("home", Path("home"), "full-auto"),
            ("hooks", nddev_cline.CLINE_HOOKS_RELATIVE, "full-auto"),
            ("runtime", Path("runtime"), "full-auto"),
            ("sandbox", nddev_cline.CLINE_SANDBOX_RELATIVE, "safe"),
        ):
            symlink_target = root / f"runtime-symlink-{name}"
            symlink_target.mkdir(mode=0o700)
            external = root / f"external-{name}"
            if name in {"hooks", "sandbox"}:
                external = root / f"missing-{name}"
            else:
                external.mkdir(mode=0o700)
            parent = symlink_target / relative.parent
            current = symlink_target
            for part in relative.parent.parts:
                current = current / part
                current.mkdir(mode=0o700, exist_ok=True)
                current.chmod(0o700)
            os.symlink(external, symlink_target / relative)
            observed_error = None
            try:
                nddev_cline.isolated_child_environment(
                    symlink_target,
                    profile_id=profile_id,
                    command_permissions={"allow": [], "deny": ["*"], "allowRedirects": False},
                )
            except nddev_cline.ClineSetupError as exc:
                observed_error = str(exc)
            require(observed_error is not None and "real directory" in observed_error, f"{name} symlink runtime path was not rejected", errors)

        stale_target = root / "stale-build"
        nddev_cline.mutate_setup(stale_target, "nddev-builder", "full-auto", "install")
        stale_canonical = stale_target.resolve()
        stamp_path = stale_canonical / nddev_cline.STAMP_NAME
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        stamp["build_version"] = "prior-build"
        stamp_path.write_bytes(nddev_cline.canonical_json(stamp))
        stamp_path.chmod(0o600)
        state = nddev_cline.inspect_target(stale_canonical)
        require(state.get("state") == "managed" and state.get("needs_update") is True, "prior-build stamp was not reported as needs_update", errors)
        plan = nddev_cline.plan_setup(stale_canonical, "nddev-builder", "full-auto")
        require(plan.get("operation") == "install", "prior-build current-schema target was not plannable", errors)
        nddev_cline.mutate_setup(stale_canonical, "nddev-builder", "full-auto", "install")
        state = nddev_cline.inspect_target(stale_canonical)
        require(state.get("needs_update") is False, "install did not refresh prior-build stamp", errors)

        legacy_target = root / "legacy-restore"
        legacy_target.mkdir(mode=0o700)
        legacy_canonical = legacy_target.resolve()
        legacy_files = {
            Path("data/settings/global-settings.json"): b"{\n  \"commandPermissions\": {\n    \"allow\": [],\n    \"allowRedirects\": false,\n    \"deny\": [\"*\"]\n  }\n}\n",
            Path("data/settings/cline_mcp_settings.json"): b"{\n  \"mcpServers\": {}\n}\n",
            Path("rules/nddev-managed.md"): b"# legacy rules\n",
        }
        for relative, content in legacy_files.items():
            destination = nddev_cline.ensure_private_parent(legacy_canonical, relative)
            destination.write_bytes(content)
            destination.chmod(0o600)
        legacy_stamp = {
            "schema_version": 1,
            "product_name": nddev_cline.PRODUCT_NAME,
            "build_version": next(iter(nddev_cline.LEGACY_BUILD_VERSIONS)),
            "setup_id": "safe",
            "canonical_target": str(legacy_canonical),
            "managed_files": {
                str(relative): nddev_cline.legacy_managed_digest(relative, content)
                for relative, content in legacy_files.items()
            },
            "builder_projection": "cline-native-skills-agents-plugin-user-files",
            "launch_args": ["--plan", "--auto-approve", "false"],
            "command_permissions": {"allow": [], "deny": ["*"], "allowRedirects": False},
        }
        stamp_file = legacy_canonical / nddev_cline.STAMP_NAME
        stamp_file.write_bytes(nddev_cline.canonical_json(legacy_stamp))
        stamp_file.chmod(0o600)
        legacy_state = nddev_cline.inspect_target(legacy_canonical)
        require(legacy_state.get("state") == "legacy-managed", "legacy target was not recognized", errors)
        nddev_cline.migrate_setup(legacy_canonical, "nddev-builder", "full-auto")
        restored = nddev_cline.restore_backup(legacy_canonical, 0)
        require(restored.get("state") == "legacy-managed", "legacy restore did not restore legacy state", errors)

        sticky_parent = root / "sticky-parent"
        sticky_parent.mkdir(mode=0o1777)
        sticky_parent.chmod(0o1777)
        sticky_target = sticky_parent / "valid-target"
        nddev_cline.mutate_setup(sticky_target, "nddev-builder", "full-auto", "install")
        state = nddev_cline.inspect_target(sticky_target.resolve())
        require(state.get("state") == "managed", "sticky-parent target did not become managed", errors)

    with tempfile.TemporaryDirectory(prefix="nddev-cline-path-") as raw_root:
        root = Path(raw_root)
        fakebin = root / "fakebin"
        fakebin.mkdir(mode=0o700)
        fake_node = fakebin / "node"
        fake_node.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
        fake_node.chmod(0o700)
        stage = root / "stage"
        live = root / "live"
        stage.mkdir(mode=0o700)
        live.mkdir(mode=0o700)
        env, _userconfig, _globalconfig, _project_dir = nddev_cline.install_stage_environment(stage, live)
        observed: dict[str, Any] = {}

        def fake_which(name: str, *, path: str | None = None) -> str:
            observed.setdefault("which", []).append({"name": name, "path": path})
            return f"/usr/bin/{name}"

        def fake_process(
            argv: list[str],
            *,
            cwd: Path,
            env: dict[str, str],
            label: str,
            timeout: int = nddev_cline.PROCESS_TIMEOUT_SECONDS,
        ) -> subprocess.CompletedProcess[str]:
            del cwd, env, label, timeout
            observed["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "v20.0.0\n", "")

        original_path = os.environ.get("PATH")
        original_which = nddev_cline.shutil.which
        original_process = nddev_cline.run_bounded_process
        os.environ["PATH"] = str(fakebin)
        nddev_cline.shutil.which = fake_which  # type: ignore[assignment]
        nddev_cline.run_bounded_process = fake_process  # type: ignore[assignment]
        try:
            nddev_cline.require_node_preflight(env, stage)
        finally:
            if original_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = original_path
            nddev_cline.shutil.which = original_which  # type: ignore[assignment]
            nddev_cline.run_bounded_process = original_process  # type: ignore[assignment]
        require(observed.get("argv", [None])[0] == "/usr/bin/node", "Node preflight did not use trusted absolute executable", errors)
        which_calls = observed.get("which", [])
        require(which_calls and which_calls[0].get("path") == nddev_cline.DETERMINISTIC_PATH, "trusted executable lookup used ambient PATH", errors)


def validate_current_sources(errors: list[str]) -> None:
    baseline = read_json("references/cline-baseline.json")
    sources = baseline.get("sources")
    require(isinstance(sources, list), "baseline sources missing", errors)
    if not isinstance(sources, list):
        return
    required = {
        "https://docs.cline.bot/getting-started/installing-cline",
        "https://docs.cline.bot/cli/cli-reference",
        "https://docs.cline.bot/customization/plugins",
        "https://docs.cline.bot/sdk/guides/writing-plugins",
        "https://raw.githubusercontent.com/cline/cline/main/apps/cli/README.md",
        "https://raw.githubusercontent.com/cline/cline/main/sdk/packages/shared/src/storage/paths.ts",
        "https://raw.githubusercontent.com/cline/cline/main/apps/cli/src/commands/config.ts",
        "https://docs.npmjs.com/cli/v11/commands/npm-ci/",
        "https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/",
        "https://registry.npmjs.org/cline/latest",
    }
    require(required.issubset(set(sources)), "current official source set missing required sources", errors)


def validate_absence_of_placeholders(errors: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or "__pycache__" in path.parts or path.is_dir() or path.suffix == ".pyc":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()
        require(PLACEHOLDER_MARKER not in lowered, f"placeholder marker found in {path.relative_to(ROOT)}", errors)
    setup_payload = (ROOT / "setups/nddev-builder/global-settings.json").read_text(encoding="utf-8")
    for forbidden in ("dangerousActions", "allowRemoteMcp", "sandbox.mode"):
        require(forbidden not in setup_payload, f"unproven settings key found in setup payload: {forbidden}", errors)


def validate_shared_ci(errors: list[str]) -> None:
    workflow_root = ROOT / ".github" / "workflows"
    require(workflow_root.is_dir(), "missing .github/workflows", errors)
    for filename, workflow in SHARED_CALLERS.items():
        path = workflow_root / filename
        require(path.is_file(), f"missing workflow {filename}", errors)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        expected = f"NDDev-it-com/ci-workflows/{workflow}@{SHARED_CI_COMMIT} # {SHARED_CI_VERSION}"
        require(text.count(expected) == 1, f"{filename} shared CI pin mismatch", errors)


def validate_release_workflow(errors: list[str]) -> None:
    tracked = repository_tracked_files()
    artifact_mode = tracked is None
    workflow = ROOT / RELEASE_WORKFLOW
    require(workflow.is_file(), "release workflow must exist", errors)
    if tracked is not None:
        require(RELEASE_WORKFLOW in tracked, "release workflow must be tracked", errors)
    if not workflow.is_file():
        return
    text = workflow.read_text(encoding="utf-8")
    expected_use = (
        "uses: NDDev-it-com/ci-workflows/.github/workflows/"
        f"release-supply-chain.yml@{SHARED_CI_COMMIT} # {SHARED_CI_VERSION}"
    )
    require(text.count(expected_use) == 1, "release workflow shared pin mismatch", errors)
    require("permissions: {}" in text, "release workflow top-level permissions must be empty", errors)
    publish_permissions = _workflow_block(text, "    permissions:")
    require(
        _workflow_scalar_mapping(publish_permissions) == REQUIRED_RELEASE_PERMISSIONS,
        "release workflow permission set mismatch",
        errors,
    )
    for name, value in REQUIRED_RELEASE_PERMISSIONS.items():
        require(
            f"      {name}: {value}" in publish_permissions,
            f"release workflow missing permission {name}: {value}",
            errors,
        )
    with_block = _workflow_block(text, "    with:")
    require(
        _workflow_mapping_keys(with_block) == REQUIRED_RELEASE_INPUTS,
        "release workflow input key set mismatch",
        errors,
    )
    for name in REQUIRED_RELEASE_INPUTS:
        require(
            any(line.strip().startswith(f"{name}:") for line in with_block),
            f"release workflow missing input {name}",
            errors,
        )
    require(
        any(line.strip() == "package_name: nddev-cline-app" for line in with_block),
        "release workflow package_name mismatch",
        errors,
    )
    archive_paths = _workflow_with_value(text, "archive_paths")
    runtime_paths = _workflow_with_value(text, "runtime_paths")
    require(archive_paths == RELEASE_ARCHIVE_PATHS, "release archive_paths mismatch", errors)
    require(runtime_paths == RELEASE_RUNTIME_PATHS, "release runtime_paths mismatch", errors)
    require(set(runtime_paths).issubset(set(archive_paths)), "runtime paths must be a subset of archive paths", errors)
    require(REQUIRED_CONTRACT_ROOTS.issubset(set(archive_paths)), "archive paths missing contract roots", errors)
    require(REQUIRED_CONTRACT_ROOTS.issubset(set(runtime_paths)), "runtime paths missing contract roots", errors)
    require(
        REQUIRED_GOVERNANCE_ARCHIVE_PATHS.issubset(set(archive_paths)),
        "archive paths missing governance source roots",
        errors,
    )
    require(
        REQUIRED_GOVERNANCE_ARCHIVE_PATHS.isdisjoint(set(runtime_paths)),
        "runtime paths must not include governance-only source roots",
        errors,
    )
    for declared in [*archive_paths, *runtime_paths]:
        path = ROOT / declared
        require(path.exists(), f"release path does not exist: {declared}", errors)
        validate_no_symlinks_under(path, f"release path {declared}", errors)
        if tracked is not None:
            covered = [
                item
                for item in tracked
                if item == declared or item.startswith(f"{declared}/")
            ]
            require(bool(covered), f"release path has no tracked files: {declared}", errors)
        for marker in PRIVATE_PATH_MARKERS:
            require(
                marker not in Path(declared).parts,
                f"release path contains private marker {marker}: {declared}",
                errors,
            )
    inventory = actual_package_files() if artifact_mode else tracked
    for item in sorted(inventory):
        parts = Path(item).parts
        for marker in PRIVATE_PATH_MARKERS:
            require(marker not in parts, f"tracked public path contains private marker {marker}: {item}", errors)
        if artifact_mode:
            require(
                _path_is_covered(item, archive_paths),
                f"artifact path is outside release archive_paths closure: {item}",
                errors,
            )


def main() -> int:
    errors: list[str] = []
    validate_bootstrap_lock_contract(errors)
    with isolated_bootstrap_root(errors):
        validate_versions(errors)
        validate_setups_and_profiles(errors)
        validate_install_lock_assets(errors)
        validate_builder(errors)
        validate_runtime_contract(errors)
        validate_bootstrap_lock_smokes(errors)
        validate_bootstrap_lock_handover(errors)
        validate_launch_profiles(errors)
        validate_npm_stage_and_timeout(errors)
        validate_security_smokes(errors)
        validate_current_sources(errors)
        validate_absence_of_placeholders(errors)
        validate_shared_ci(errors)
        validate_release_workflow(errors)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("nddev-cline-app public contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
