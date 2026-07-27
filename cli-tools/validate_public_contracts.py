#!/usr/bin/env python3
"""Validate nddev-cline-app public contracts without live Cline side effects."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
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
SHARED_CALLERS = {
    "actionlint.yml": ".github/workflows/actionlint.yml",
    "codeql.yml": ".github/workflows/public-codeql.yml",
    "dependency-review.yml": ".github/workflows/public-dependency-review.yml",
    "release.yml": ".github/workflows/release-supply-chain.yml",
    "scorecard.yml": ".github/workflows/public-scorecard-json.yml",
    "secret-scan.yml": ".github/workflows/secret-scan.yml",
    "zizmor.yml": ".github/workflows/zizmor-sarif.yml",
}
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


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/cline-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(manifest.get("name") == ROOT.name, "manifest name mismatch", errors)
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
        require(launch.get("executable_source") == "validated-target-owned-npm-global-prefix-install", "runtime executable source mismatch", errors)
        require(launch.get("blocks_user_managed_flags") == EXPECTED["blocked_launch_flags"], "contract launch flag blocklist mismatch", errors)
        require("legacy" in launch.get("legacy_launch_policy", ""), "legacy launch policy missing", errors)
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
            if isinstance(npm, dict):
                registry_preflight = cli.get("registry_metadata_preflight", {})
                require(registry_preflight.get("integrity") == npm.get("integrity"), "registry integrity mismatch", errors)
                require(registry_preflight.get("shasum") == npm.get("shasum"), "registry shasum mismatch", errors)
                require(registry_preflight.get("tarball") == npm.get("tarball"), "registry tarball mismatch", errors)
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
        require(lifecycle.get("install_argv", [None])[0] == "npm", "manifest install argv must use npm", errors)
        expected_node_preflight = (
            f"Node.js {nddev_cline.MIN_NODE_MAJOR}+ required; "
            f"{nddev_cline.RECOMMENDED_NODE_MAJOR} recommended"
        )
        require(lifecycle.get("node_preflight") == expected_node_preflight, "manifest Node preflight mismatch", errors)
        bounds = lifecycle.get("bounds")
        require(isinstance(bounds, dict), "manifest software bounds missing", errors)
        if isinstance(bounds, dict):
            require(bounds.get("max_tree_paths") == nddev_cline.SOFTWARE_TREE_MAX_PATHS, "software path bound mismatch", errors)
            require(bounds.get("max_tree_bytes") == nddev_cline.SOFTWARE_TREE_MAX_BYTES, "software byte bound mismatch", errors)


def validate_launch_profiles(errors: list[str]) -> None:
    captures: list[dict[str, Any]] = []

    def fake_run(argv: list[str], *, cwd: str, env: dict[str, str], check: bool, timeout: None) -> subprocess.CompletedProcess[str]:
        del cwd, check, timeout
        captures.append({"argv": argv, "env": env})
        return subprocess.CompletedProcess(argv, 0, "", "")

    original_status = nddev_cline.software_status
    original_run = subprocess.run
    try:
        nddev_cline.software_status = lambda target: {  # type: ignore[assignment]
            "ok": True,
            "installed": True,
            "current": True,
            "target": str(target),
        }
        subprocess.run = fake_run  # type: ignore[assignment]
        with tempfile.TemporaryDirectory(prefix="nddev-cline-launch-") as raw:
            root = Path(raw)
            target = root / "target"
            target.parent.chmod(0o700)
            nddev_cline.mutate_setup(target, "nddev-builder", "full-auto", "install")
            canonical_target = target.resolve()
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
            require("CLINE_DATA_DIR" not in env, "full-auto must not set CLINE_DATA_DIR", errors)
            require("CLINE_SANDBOX" not in env, "full-auto must not set CLINE_SANDBOX", errors)
            require(env.get("HOME") == str(canonical_target / "home"), "full-auto HOME mismatch", errors)
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
            require(env.get("CLINE_SANDBOX") == "1", "safe must set CLINE_SANDBOX=1", errors)
            require("CLINE_DATA_DIR" not in env, "safe should let --data-dir drive data dir", errors)
    finally:
        nddev_cline.software_status = original_status  # type: ignore[assignment]
        subprocess.run = original_run  # type: ignore[assignment]


def validate_npm_stage_and_timeout(errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-cline-public-regression-") as raw_root:
        root = Path(raw_root)
        stage = root / "stage"
        live = root / "live"
        stage.mkdir(mode=0o700)
        live.mkdir(mode=0o700)
        env, userconfig, globalconfig, global_dir = nddev_cline.install_stage_environment(stage, live)
        require(env.get("NPM_CONFIG_PREFIX") == str(global_dir), "npm prefix env mismatch", errors)
        require(env.get("NPM_CONFIG_CACHE") == str(stage / "cache"), "npm cache env mismatch", errors)
        require(env.get("NPM_CONFIG_USERCONFIG") == str(userconfig), "npm userconfig env mismatch", errors)
        require(env.get("NPM_CONFIG_GLOBALCONFIG") == str(globalconfig), "npm globalconfig env mismatch", errors)
        require("CLINE_DATA_DIR" not in env and "CLINE_SANDBOX" not in env, "npm stage must not set Cline runtime env", errors)
        npmrc = userconfig.read_text(encoding="utf-8")
        require(f"registry={nddev_cline.NPM_REGISTRY}" in npmrc, "npmrc registry mismatch", errors)
        require("prefix=" in npmrc and str(global_dir) in npmrc, "npmrc prefix mismatch", errors)
        require("auth" not in npmrc.lower() and "token" not in npmrc.lower(), "npmrc must not contain auth material", errors)
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
        require(not nddev_cline.lock_path(target).exists(), "npm timeout left target lock behind", errors)
        require(not list(root.glob(".target.nddev-cline-cli-stage.*")), "npm timeout left staging directory behind", errors)


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


def main() -> int:
    errors: list[str] = []
    validate_versions(errors)
    validate_setups_and_profiles(errors)
    validate_builder(errors)
    validate_runtime_contract(errors)
    validate_launch_profiles(errors)
    validate_npm_stage_and_timeout(errors)
    validate_current_sources(errors)
    validate_absence_of_placeholders(errors)
    validate_shared_ci(errors)
    if errors:
        for error in errors:
            print(error)
        return 1
    print("nddev-cline-app public contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
