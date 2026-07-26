#!/usr/bin/env python3
"""Validate nddev-cline-app public contracts without side effects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+].*)?\Z")
SETUP_IDS = ["safe", "balanced", "full-auto"]
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
    "cli_version": "3.0.46",
    "cli_package": "cline",
    "cli_integrity": "sha512-U6uH3sLVvqx4fP65ejHkswhk3WvYOM2LCbQBX77Z7Tha4EX35vo2XZ51F6WnIiKlCAYZJ+YAEou2Yha/EAk+2A==",
    "cli_shasum": "5b731496d251f76448fe676edbf4fde415b58881",
    "extension_version": "4.0.11",
    "extension_id": "saoudrizwan.claude-dev",
    "release_tag": "v4.0.11",
    "release_published_at": "2026-07-24T19:03:48Z",
    "vsix_sha256": "a4641d8bc47f766300203cee0c4e1d84f690a88586b43e81ed015bba9af79a2d",
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


def validate_versions(errors: list[str]) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    build = read_json("build/version.json")
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    baseline = read_json("references/cline-baseline.json")
    require(SEMVER.fullmatch(version) is not None, "VERSION is not SemVer", errors)
    require(version != "0.0.0", "VERSION must not be placeholder 0.0.0", errors)
    require(build.get("schema_version") == 2, "build schema mismatch", errors)
    require(manifest.get("schema_version") == 2, "manifest schema mismatch", errors)
    require(contract.get("contract_version") == 2, "contract version mismatch", errors)
    require(build.get("build_version") == version, "build version mismatch", errors)
    require(manifest.get("build_version") == version, "manifest version mismatch", errors)
    require(build.get("cline_cli_tested") == EXPECTED["cli_version"], "CLI version mismatch", errors)
    require(build.get("cline_cli_package") == EXPECTED["cli_package"], "CLI package mismatch", errors)
    require(build.get("cline_cli_integrity") == EXPECTED["cli_integrity"], "CLI integrity mismatch", errors)
    require(build.get("cline_cli_shasum") == EXPECTED["cli_shasum"], "CLI shasum mismatch", errors)
    require(build.get("cline_extension_tested") == EXPECTED["extension_version"], "extension version mismatch", errors)
    require(build.get("vscode_extension_id") == EXPECTED["extension_id"], "extension id mismatch", errors)
    require(build.get("cline_extension_release_tag") == EXPECTED["release_tag"], "release tag mismatch", errors)
    require(build.get("cline_extension_published_at") == EXPECTED["release_published_at"], "release published_at mismatch", errors)
    require(build.get("vscode_extension_vsix_sha256") == EXPECTED["vsix_sha256"], "VSIX digest mismatch", errors)
    npm = baseline.get("npm")
    extension = baseline.get("extension")
    release = baseline.get("release")
    require(isinstance(npm, dict), "baseline npm missing", errors)
    require(isinstance(extension, dict), "baseline extension missing", errors)
    require(isinstance(release, dict), "baseline release missing", errors)
    if isinstance(npm, dict):
        require(npm.get("version") == build.get("cline_cli_tested"), "baseline npm version mismatch", errors)
        require(npm.get("integrity") == build.get("cline_cli_integrity"), "baseline npm integrity mismatch", errors)
        require(npm.get("shasum") == build.get("cline_cli_shasum"), "baseline npm shasum mismatch", errors)
    if isinstance(extension, dict):
        require(extension.get("id") == build.get("vscode_extension_id"), "baseline extension id mismatch", errors)
        require(extension.get("version") == build.get("cline_extension_tested"), "baseline extension version mismatch", errors)
        require(extension.get("install_supported_by_manager") is False, "extension install must be unsupported", errors)
        require(extension.get("launch_supported_by_manager") is False, "extension launch must be unsupported", errors)
    if isinstance(release, dict):
        require(release.get("tag") == build.get("cline_extension_release_tag"), "baseline release tag mismatch", errors)
        require(release.get("assets", {}).get("vscode-vsix", {}).get("sha256") == build.get("vscode_extension_vsix_sha256"), "baseline VSIX digest mismatch", errors)
    runtime = contract.get("runtime_compatibility")
    require(isinstance(runtime, dict), "contract runtime_compatibility missing", errors)
    if isinstance(runtime, dict):
        require(runtime.get("cli_tested_version") == build.get("cline_cli_tested"), "contract CLI version mismatch", errors)
        require(runtime.get("extension_tested_version") == build.get("cline_extension_tested"), "contract extension version mismatch", errors)


def validate_command_permissions(value: Any, label: str, errors: list[str]) -> None:
    require(isinstance(value, dict), f"{label} command permissions missing", errors)
    if not isinstance(value, dict):
        return
    require(isinstance(value.get("allow"), list), f"{label} allow must be array", errors)
    require(isinstance(value.get("deny"), list), f"{label} deny must be array", errors)
    require(isinstance(value.get("allowRedirects"), bool), f"{label} allowRedirects must be bool", errors)
    for key in ("allow", "deny"):
        items = value.get(key)
        if isinstance(items, list):
            require(all(isinstance(item, str) for item in items), f"{label} {key} must contain strings", errors)


def validate_setups(errors: list[str]) -> None:
    manifest = read_json("build/manifest.json")
    contract = read_json("config/nddev-contract.json")
    require(manifest.get("setup_ids") == SETUP_IDS, "manifest setup ids mismatch", errors)
    setup_system = contract.get("setup_system")
    require(isinstance(setup_system, dict), "contract setup_system missing", errors)
    if isinstance(setup_system, dict):
        require(setup_system.get("setup_ids") == SETUP_IDS, "contract setup ids mismatch", errors)
        require(setup_system.get("builder_default_on") is True, "builder default mismatch", errors)
    expected_args = {
        "safe": ["--plan", "--auto-approve", "false"],
        "balanced": ["--auto-approve", "false"],
        "full-auto": ["--auto-approve", "true"],
    }
    for setup_id in SETUP_IDS:
        metadata = read_json(f"setups/{setup_id}/setup.json")
        settings = read_json(f"setups/{setup_id}/global-settings.json")
        mcp = read_json(f"setups/{setup_id}/cline_mcp_settings.json")
        require(metadata.get("id") == setup_id, f"{setup_id} id mismatch", errors)
        require(metadata.get("launch_args") == expected_args[setup_id], f"{setup_id} launch args mismatch", errors)
        require(metadata.get("builder_default_on") is True, f"{setup_id} builder not default-on", errors)
        require(metadata.get("builder_projection") == "native-skills-agents-plugin-user-files", f"{setup_id} builder projection mismatch", errors)
        require(settings.get("cline", {}).get("autoUpdate") is False, f"{setup_id} autoupdate must be disabled", errors)
        require(settings.get("cline", {}).get("dataDir") == "${CLINE_DATA_DIR}", f"{setup_id} dataDir mismatch", errors)
        require(settings.get("cline", {}).get("plugins") == {"enabled": ["nddev-builder"]}, f"{setup_id} builder plugin not enabled", errors)
        require(settings.get("mcp") == {"servers": {}}, f"{setup_id} MCP must be empty", errors)
        require(mcp == {"mcpServers": {}}, f"{setup_id} MCP settings must be empty", errors)
        require(settings.get("telemetry") == {"enabled": False}, f"{setup_id} telemetry must be off", errors)
        validate_command_permissions(settings.get("commandPermissions"), setup_id, errors)


def validate_builder(errors: list[str]) -> None:
    contract = read_json("config/nddev-contract.json")
    build = read_json("build/version.json")
    package_json = read_json("plugins/nddev-builder/plugins/nddev-builder/package.json")
    builder = contract.get("builder_capability")
    require(isinstance(builder, dict), "contract builder missing", errors)
    if isinstance(builder, dict):
        require(builder.get("projection") == "cline-native-skills-agents-plugin-user-files", "builder projection mismatch", errors)
        require(builder.get("default_on") is True, "builder default_on mismatch", errors)
        require(builder.get("marketplace") is None, "builder marketplace must be null", errors)
        require(builder.get("version") == build.get("nddev_builder_extension_version"), "builder version mismatch", errors)
    require(package_json.get("name") == "nddev-builder", "builder package name mismatch", errors)
    require(package_json.get("version") == build.get("nddev_builder_extension_version"), "builder package version mismatch", errors)
    require("cline" in package_json, "builder package missing cline field", errors)
    for relative in (
        "plugins/nddev-builder/skills/nddev-builder/SKILL.md",
        "plugins/nddev-builder/agents/nddev-builder.md",
        "plugins/nddev-builder/plugins/nddev-builder/index.js",
    ):
        require((ROOT / relative).is_file(), f"builder native file missing: {relative}", errors)


def validate_runtime_contract(errors: list[str]) -> None:
    contract = read_json("config/nddev-contract.json")
    launch = contract.get("runtime_launch")
    software = contract.get("software_install")
    require(isinstance(launch, dict), "runtime_launch missing", errors)
    require(isinstance(software, dict), "software_install missing", errors)
    if isinstance(launch, dict):
        require(launch.get("extension_launch_supported") is False, "extension launch must be unsupported", errors)
        require(launch.get("extension_install_supported") is False, "extension install must be unsupported", errors)
        require(launch.get("token_environment_inheritance") == "stripped", "tokens must be stripped", errors)
    if isinstance(software, dict):
        cli = software.get("cli")
        extension = software.get("extension")
        require(isinstance(cli, dict) and cli.get("supported") is True, "CLI install must be supported", errors)
        require(isinstance(extension, dict) and extension.get("supported") is False, "extension install must be unsupported", errors)


def validate_current_sources(errors: list[str]) -> None:
    baseline = read_json("references/cline-baseline.json")
    sources = baseline.get("sources")
    require(isinstance(sources, list), "baseline sources missing", errors)
    if not isinstance(sources, list):
        return
    expected_sources = {
        "https://docs.cline.bot/getting-started/installing-cline",
        "https://docs.cline.bot/getting-started/config",
        "https://docs.cline.bot/cli/cli-reference",
        "https://docs.cline.bot/customization/skills",
        "https://docs.cline.bot/customization/plugins",
        "https://docs.cline.bot/mcp/mcp-overview",
        "https://github.com/cline/cline/releases/tag/v4.0.11",
        "https://marketplace.visualstudio.com/items?itemName=saoudrizwan.claude-dev",
        "https://registry.npmjs.org/cline",
    }
    require(set(sources) == expected_sources, "current official source set mismatch", errors)


def validate_absence_of_placeholders(errors: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts or path.is_dir():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        require(
            PLACEHOLDER_MARKER not in text,
            f"placeholder marker found in {path.relative_to(ROOT)}",
            errors,
        )


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
    validate_setups(errors)
    validate_builder(errors)
    validate_runtime_contract(errors)
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
