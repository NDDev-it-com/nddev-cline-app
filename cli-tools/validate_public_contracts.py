#!/usr/bin/env python3
"""Validate nddev-cline-app public contracts without live Cline side effects."""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
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
    ".claude/CLAUDE.md",
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
    "AGENTS.md",
    ".claude/CLAUDE.md",
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
    ".claude/CLAUDE.md",
    ".gds/repository.yaml",
}
REQUIRED_RUNTIME_INSTRUCTION_PATHS = {
    "AGENTS.md",
    ".claude/CLAUDE.md",
}
REQUIRED_SOURCE_ONLY_GOVERNANCE_PATHS = {
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


def expected_managed_files() -> set[str]:
    return {str(path) for path in nddev_cline.MANAGED_PATHS}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            keys = keys | {line.strip().split(":", 1)[0]}
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
            files = files | {relative.as_posix()}
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
            optional_seen = optional_seen | {package_name}
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
    skill_root = ROOT / "plugins/nddev-builder/skills/nddev-builder"
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
    local_reference_pattern = re.compile(r"`((?:\./)?references/[A-Za-z0-9._/-]+)`")
    for markdown in sorted(skill_root.rglob("*.md")):
        text = markdown.read_text(encoding="utf-8")
        require(
            "references/cline-baseline.json" not in text,
            f"builder skill must not point at module-root volatile baseline: {markdown.relative_to(skill_root)}",
            errors,
        )
        for match in local_reference_pattern.finditer(text):
            candidate = match.group(1).removeprefix("./")
            path = skill_root / candidate
            require(
                path.is_file(),
                f"builder skill unresolved local reference {candidate}: {markdown.relative_to(skill_root)}",
                errors,
            )


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
            require("product global.lock" in lock_policy, "launch policy must use the product global lock", errors)
            require("canonical-target anchor" in lock_policy, "launch policy must use canonical target anchors", errors)
            require("Read-only status, plan, and software-status never create" in lock_policy, "launch policy must declare read-only no-create coordination", errors)
            require("fixed resolved system temp root" in lock_policy, "launch policy must bind bootstrap lock to fixed system temp", errors)
            require("never removed by normal lifecycle cleanup" in lock_policy, "launch policy must keep external anchors persistent", errors)
            require("product external, canonical external, then internal" in lock_policy, "launch policy must document lock acquisition order", errors)
            require("internal before canonical external" in lock_policy, "launch policy must document lock release order", errors)
            require("renames the internal lock directory" in lock_policy, "launch policy must cover internal lock directory rename", errors)
            require("bootstrap lock root" in lock_policy, "launch policy must disclose bootstrap root same-UID boundary", errors)
            require("target root, HOME, config, TMP, XDG, runtime, and sandbox directories remain writable" in lock_policy, "launch policy must preserve runtime writability", errors)
            require("--data-dir" not in launch.get("full_auto_command", ""), "full-auto command must not include --data-dir", errors)
            require("CLINE_SANDBOX" not in launch.get("full_auto_command", ""), "full-auto command must not set sandbox env", errors)
            require(
                "cleanup is pending" in (ROOT / "cli-tools" / "nddev_cline.py").read_text(encoding="utf-8"),
                "launch must fail closed while cleanup is pending",
                errors,
            )
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
    if isinstance(transaction, dict):
        require(
            transaction.get("cleanup_journal_schema") == nddev_cline.CLEANUP_SCHEMA_VERSION,
            "contract cleanup journal schema mismatch",
            errors,
        )
        cleanup_policy = transaction.get("cleanup_pending")
        require(
            isinstance(cleanup_policy, str)
            and "read-only commands expose valid pending state without repair" in cleanup_policy
            and "launch fails closed" in cleanup_policy,
            "contract cleanup pending policy mismatch",
            errors,
        )
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
        require(
            lifecycle.get("cleanup_journal_schema") == nddev_cline.CLEANUP_SCHEMA_VERSION,
            "manifest cleanup journal schema mismatch",
            errors,
        )
        cleanup_policy = lifecycle.get("cleanup_pending")
        require(
            isinstance(cleanup_policy, str)
            and "launch fails closed" in cleanup_policy
            and "drains it before active changes" in cleanup_policy,
            "manifest cleanup pending policy mismatch",
            errors,
        )
        expected_node_preflight = (
            f"Node.js {nddev_cline.MIN_NODE_MAJOR}+ required; "
            f"{nddev_cline.RECOMMENDED_NODE_MAJOR} recommended"
        )
        require(lifecycle.get("node_preflight") == expected_node_preflight, "manifest Node preflight mismatch", errors)
        handoff = lifecycle.get("launch_handoff_policy")
        require(
            isinstance(handoff, str)
            and "path-based spawn" in handoff
            and "product global.lock" in handoff
            and "canonical-target" in handoff
            and "no-create" in handoff
            and "fixed system temp" in handoff
            and "acquisition product external/canonical external/internal" in handoff
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
    inventory = actual_package_files()
    workflow = ROOT / RELEASE_WORKFLOW
    require(workflow.is_file(), "release workflow must exist", errors)
    require(RELEASE_WORKFLOW in inventory, "release workflow must be packaged", errors)
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
        REQUIRED_RUNTIME_INSTRUCTION_PATHS.issubset(set(runtime_paths)),
        "runtime paths missing instruction roots",
        errors,
    )
    require(
        REQUIRED_SOURCE_ONLY_GOVERNANCE_PATHS.isdisjoint(set(runtime_paths)),
        "runtime paths must not include source-only governance roots",
        errors,
    )
    for declared in [*archive_paths, *runtime_paths]:
        path = ROOT / declared
        require(path.exists(), f"release path does not exist: {declared}", errors)
        validate_no_symlinks_under(path, f"release path {declared}", errors)
        covered = [
            item
            for item in inventory
            if item == declared or item.startswith(f"{declared}/")
        ]
        require(bool(covered), f"release path has no packaged files: {declared}", errors)
        for marker in PRIVATE_PATH_MARKERS:
            require(
                marker not in Path(declared).parts,
                f"release path contains private marker {marker}: {declared}",
                errors,
            )
    for item in sorted(inventory):
        parts = Path(item).parts
        for marker in PRIVATE_PATH_MARKERS:
            require(marker not in parts, f"tracked public path contains private marker {marker}: {item}", errors)
        require(
            _path_is_covered(item, archive_paths),
            f"artifact path is outside release archive_paths closure: {item}",
            errors,
        )


def validate_claude_bridge(errors: list[str]) -> None:
    claude_dir = ROOT / ".claude"
    agents = ROOT / "AGENTS.md"
    bridge = ROOT / ".claude" / "CLAUDE.md"
    try:
        claude_info = claude_dir.lstat()
    except FileNotFoundError:
        require(False, ".claude directory must exist", errors)
        claude_info = None
    if claude_info is not None:
        require(
            stat.S_ISDIR(claude_info.st_mode) and not stat.S_ISLNK(claude_info.st_mode),
            ".claude must be a real directory",
            errors,
        )
        if stat.S_ISDIR(claude_info.st_mode) and not stat.S_ISLNK(claude_info.st_mode):
            require(
                sorted(path.name for path in claude_dir.iterdir()) == ["CLAUDE.md"],
                ".claude must contain only CLAUDE.md",
                errors,
            )
    try:
        agents_info = agents.lstat()
    except FileNotFoundError:
        require(False, "AGENTS.md must exist for Claude bridge import", errors)
    else:
        require(
            stat.S_ISREG(agents_info.st_mode) and not stat.S_ISLNK(agents_info.st_mode),
            "AGENTS.md must be a real regular file",
            errors,
        )
    require(bridge.is_file(), ".claude/CLAUDE.md bridge must exist", errors)
    if bridge.exists():
        bridge_info = bridge.lstat()
        require(
            stat.S_ISREG(bridge_info.st_mode) and not stat.S_ISLNK(bridge_info.st_mode),
            ".claude/CLAUDE.md bridge must be a regular non-symlink file",
            errors,
        )
        require(
            bridge.read_bytes() == b"@../AGENTS.md\n",
            ".claude/CLAUDE.md bridge must exactly import AGENTS.md",
            errors,
        )


def main() -> int:
    errors: list[str] = []
    validate_bootstrap_lock_contract(errors)
    validate_versions(errors)
    validate_setups_and_profiles(errors)
    validate_install_lock_assets(errors)
    validate_builder(errors)
    validate_runtime_contract(errors)
    validate_current_sources(errors)
    validate_absence_of_placeholders(errors)
    validate_shared_ci(errors)
    validate_release_workflow(errors)
    validate_claude_bridge(errors)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("nddev-cline-app public contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
