#!/usr/bin/env python3
"""h2c-manager — lightweight package manager for helmfile2compose.

Downloads h2c-core (helmfile2compose.py) and optional extensions
from GitHub, resolving versions and dependencies automatically.

Usage:
    python3 h2c-manager.py                        # install core (+ depends from yaml)
    python3 h2c-manager.py keycloak                # install core + keycloak operator
    python3 h2c-manager.py keycloak==0.1.0         # pin extension version
    python3 h2c-manager.py --core-version v2.0.0   # pin core version
    python3 h2c-manager.py -d ./tools keycloak     # custom install dir
    python3 h2c-manager.py --no-reinstall          # skip download, use cached .h2c/
    python3 h2c-manager.py --info                  # show info for all extensions (or yaml depends)
    python3 h2c-manager.py --info nginx traefik    # show info for specific extensions
    python3 h2c-manager.py run -e compose          # run h2c with smart defaults

By default, h2c-core and extensions are always re-downloaded (overwriting
any cached files). Use --no-reinstall to skip the download and reuse
the existing .h2c/ directory.

If no extensions are given on the command line and a helmfile2compose.yaml
file exists in the current directory with a 'depends' list, those extensions
are installed automatically.
"""

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CORE_REPO = "helmfile2compose/h2c-core"
CORE_FILE = "helmfile2compose.py"
REGISTRY_URL = (
    "https://raw.githubusercontent.com/"
    "helmfile2compose/h2c-manager/main/extensions.json"
)
GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _github_get(url):
    """GET a GitHub API or raw URL, return bytes. Raises on HTTP error."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "h2c-manager",
    })
    with urllib.request.urlopen(req) as resp:
        return resp.read()


def _github_json(url):
    """GET a GitHub API URL, return parsed JSON."""
    return json.loads(_github_get(url))


def _latest_tag(repo):
    """Resolve the latest release tag for a GitHub repo."""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        data = _github_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: no releases found for {repo}", file=sys.stderr)
            print(f"  URL: {url}", file=sys.stderr)
            sys.exit(1)
        raise
    return data["tag_name"]


def _raw_url(repo, tag, path):
    """Build a raw.githubusercontent.com download URL."""
    return f"{RAW_BASE}/{repo}/refs/tags/{tag}/{path}"


def _release_asset_url(repo, tag, filename):
    """Build a GitHub release asset download URL."""
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def _download(url):
    """Download a URL, return bytes. Returns None on 404."""
    try:
        return _github_get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _download_or_die(url):
    """Download a URL, return bytes. Exit on 404."""
    try:
        return _github_get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("Error: file not found", file=sys.stderr)
            print(f"  URL: {url}", file=sys.stderr)
            sys.exit(1)
        raise


# ---------------------------------------------------------------------------
# helmfile2compose.yaml — depends resolution
# ---------------------------------------------------------------------------

def _read_yaml_config(yaml_path="helmfile2compose.yaml"):
    """Read 'depends' list and 'core_version' from helmfile2compose.yaml.

    Line parser — no pyyaml needed. Returns (depends_list, core_version_or_none).
    """
    if not os.path.isfile(yaml_path):
        return [], None
    in_depends = False
    depends = []
    core_version = None
    with open(yaml_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # core_version: v2.0.0
            if stripped.startswith("core_version:"):
                val = stripped.split(":", 1)[1].strip().strip("'\"")
                if val:
                    core_version = val
                continue
            # Start of depends block
            if stripped == "depends:" or stripped.startswith("depends:"):
                if "[" in stripped:
                    continue  # inline list not supported
                in_depends = True
                continue
            if in_depends:
                if stripped.startswith("- "):
                    val = stripped[2:].strip().strip("'\"")
                    if val:
                        depends.append(val)
                elif stripped == "" or stripped.startswith("#"):
                    continue
                else:
                    in_depends = False  # next YAML key — end of depends block
    return depends, core_version


# ---------------------------------------------------------------------------
# Extension registry
# ---------------------------------------------------------------------------

def _fetch_registry():
    """Fetch and parse the extensions.json registry."""
    try:
        data = _github_get(REGISTRY_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("Error: extension registry not found", file=sys.stderr)
            print(f"  URL: {REGISTRY_URL}", file=sys.stderr)
            sys.exit(1)
        raise
    registry = json.loads(data)
    return registry.get("extensions", {})


# ---------------------------------------------------------------------------
# Version / dependency resolution
# ---------------------------------------------------------------------------

def _parse_extension_arg(arg):
    """Parse 'name' or 'name==version' into (name, version_or_none)."""
    if "==" in arg:
        name, version = arg.split("==", 1)
        return name.strip(), version.strip()
    return arg.strip(), None


def _normalize_tag(version):
    """Ensure version string has a 'v' prefix for tag resolution."""
    if not version.startswith("v"):
        return f"v{version}"
    return version


def _resolve_dependencies(requested, registry):
    """Expand one-level depends for each requested extension. Preserves order.

    Returns a list of (name, pinned_version_or_none, is_dependency) tuples.
    """
    seen = set()
    result = []

    for name, pinned in requested:
        entry = registry.get(name)
        if entry is None:
            print(f"Error: unknown extension '{name}'", file=sys.stderr)
            print(f"  Available: {', '.join(sorted(registry))}", file=sys.stderr)
            sys.exit(1)

        # Add dependencies first (unpinned — latest)
        for dep in entry.get("depends", []):
            if dep not in seen:
                seen.add(dep)
                if dep not in registry:
                    print(f"Error: extension '{name}' depends on '{dep}', "
                          f"which is not in the registry", file=sys.stderr)
                    sys.exit(1)
                result.append((dep, None, True))

        # Add the extension itself
        if name not in seen:
            seen.add(name)
            result.append((name, pinned, False))

    return result


def _latest_tag_safe(repo):
    """Resolve the latest release tag, return None on failure."""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        data = _github_json(url)
        return data["tag_name"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError):
        return None


def _check_incompatible(resolved, registry, ignored=None):
    """Check for incompatible extension pairs. Exit 1 if any conflict found.

    Bidirectional: if A declares incompatible with B, both A+B and B+A trigger.
    Extensions named in 'ignored' have their conflicts bypassed.
    """
    ignored = ignored or set()
    resolved_names = {name for name, _, _ in resolved}
    for name, _, _ in resolved:
        entry = registry.get(name, {})
        for incompat in entry.get("incompatible", []):
            if incompat in resolved_names and name not in ignored and incompat not in ignored:
                print(f"Error: extensions '{name}' and '{incompat}' are incompatible",
                      file=sys.stderr)
                print(f"  Use --ignore-compatibility-errors {name} to override",
                      file=sys.stderr)
                sys.exit(1)


def _resolve_extension_version(pinned, registry_entry):
    """Resolve the tag for an extension. Returns (tag, version_display)."""
    if pinned:
        tag = _normalize_tag(pinned)
        return tag, tag
    tag = _latest_tag(registry_entry["repo"])
    return tag, tag


# ---------------------------------------------------------------------------
# Dependency checking (importlib.metadata)
# ---------------------------------------------------------------------------

def _check_requirements(extensions_with_reqs):
    """Check if Python dependencies are installed.

    extensions_with_reqs: list of (extension_name, [requirement_lines])
    Returns list of (extension_name, missing_requirement_line) tuples.
    """
    missing = []
    for ext_name, requirements in extensions_with_reqs:
        for req_line in requirements:
            req_line = req_line.strip()
            if not req_line or req_line.startswith("#"):
                continue
            # Extract package name (before any version specifier)
            pkg = req_line.split(">=")[0].split("<=")[0].split("==")[0]
            pkg = pkg.split("!=")[0].split("~=")[0].split(">")[0].split("<")[0]
            pkg = pkg.strip()
            try:
                importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                missing.append((ext_name, req_line))
    return missing


# ---------------------------------------------------------------------------
# File writing
# ---------------------------------------------------------------------------

def _write_file(path, content):
    """Write bytes to a file, creating parent directories."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)


def _fetch_file(url, local_path, label):
    """Download url to local_path. Overwrites if exists."""
    print(f"Fetching {label}...")
    content = _download_or_die(url)
    _write_file(local_path, content)
    print(f"Wrote {local_path}")


# ---------------------------------------------------------------------------
# Run mode
# ---------------------------------------------------------------------------

def _find_dependents(name, requested, registry):
    """Return a display string like ' (dependency of x, y)' or empty."""
    dependents = [req_name for req_name, _ in requested
                  if name in registry.get(req_name, {}).get("depends", [])]
    if dependents:
        return f" (dependency of {', '.join(dependents)})"
    return ""


def _install_core(install_dir, core_version, yaml_core_version, no_reinstall):
    """Download h2c-core into install_dir. Skips if cached and no_reinstall."""
    core_path = os.path.join(install_dir, CORE_FILE)
    if no_reinstall and os.path.isfile(core_path):
        print(f"Cached {core_path}")
        return
    if core_version:
        core_tag = _normalize_tag(core_version)
    elif yaml_core_version:
        core_tag = _normalize_tag(yaml_core_version)
        print(f"Core version from helmfile2compose.yaml: {core_tag}")
    else:
        core_tag = _latest_tag(CORE_REPO)
    core_url = _release_asset_url(CORE_REPO, core_tag, CORE_FILE)
    _fetch_file(core_url, core_path, f"h2c-core {core_tag}")


def _validate_extensions(requested, ignored=None):
    """Fetch registry, resolve dependencies, return (registry, resolved).

    Exits with an error if any extension is unknown or incompatible.
    Call this before downloading the core so we fail fast on bad input.
    """
    if not requested:
        return None, []
    registry = _fetch_registry()
    resolved = _resolve_dependencies(requested, registry)
    _check_incompatible(resolved, registry, ignored=ignored)
    return registry, resolved


def _install_extensions(extensions_dir, registry, resolved, requested,
                        no_reinstall):
    """Download extensions into extensions_dir.

    Takes pre-validated (registry, resolved) from _validate_extensions.

    Returns list of (name, [requirement_lines]) for newly downloaded
    extensions (cached ones are skipped, including their requirements).
    """
    extensions_with_reqs = []
    if not resolved:
        return extensions_with_reqs

    for name, pinned, is_dep in resolved:
        entry = registry[name]
        local_name = os.path.basename(entry["file"])
        ext_path = os.path.join(extensions_dir, local_name)

        if no_reinstall and os.path.isfile(ext_path):
            print(f"Cached {ext_path}")
            continue

        tag, version_display = _resolve_extension_version(pinned, entry)
        dep_of = ""
        if is_dep:
            dep_of = _find_dependents(name, requested, registry)

        file_url = _raw_url(entry["repo"], tag, entry["file"])
        _fetch_file(file_url, ext_path,
                    f"extension {name} {version_display}{dep_of}")

        reqs_url = _raw_url(entry["repo"], tag, "requirements.txt")
        reqs_data = _download(reqs_url)
        if reqs_data:
            req_lines = reqs_data.decode("utf-8").splitlines()
            extensions_with_reqs.append((name, req_lines))

    return extensions_with_reqs


def _install(core_version=None, extensions=None, install_dir=".h2c",
             no_reinstall=False, ignored=None):
    """Install h2c-core and optional extensions.

    If core_version/extensions are not given, reads from helmfile2compose.yaml.

    When no_reinstall is True, existing files are kept as-is and only missing
    files are downloaded. There is no version tracking: a cached file is never
    updated unless you run without --no-reinstall (the default).
    """
    yaml_depends, yaml_core_version = _read_yaml_config()

    ext_args = extensions if extensions is not None else []
    if not ext_args and yaml_depends:
        print(f"Reading extensions from helmfile2compose.yaml: "
              f"{', '.join(yaml_depends)}")
        ext_args = yaml_depends

    requested = [_parse_extension_arg(ext) for ext in ext_args]
    registry, resolved = _validate_extensions(requested, ignored=ignored)

    _install_core(install_dir, core_version, yaml_core_version, no_reinstall)

    extensions_dir = os.path.join(install_dir, "extensions")
    extensions_with_reqs = _install_extensions(
        extensions_dir, registry, resolved, requested, no_reinstall)

    all_checks = [("h2c-core", ["pyyaml"])]
    all_checks.extend(extensions_with_reqs)
    missing = _check_requirements(all_checks)
    if missing:
        print(file=sys.stderr)
        print("Missing Python dependencies:", file=sys.stderr)
        for component, req_line in missing:
            print(f"  {component}: {req_line}", file=sys.stderr)
        all_reqs = sorted(set(r for _, r in missing))
        print(f"\nInstall with: pip install {' '.join(all_reqs)}",
              file=sys.stderr)
        print(file=sys.stderr)


def _run(extra_args, no_reinstall=False, core_version=None, ignored=None):
    """Run helmfile2compose.py with smart defaults.

    Downloads h2c-core (+ extensions from helmfile2compose.yaml) before
    every run, overwriting cached files. Use --no-reinstall to skip
    already-cached files (missing files are still downloaded).
    Defaults: --helmfile-dir . --extensions-dir .h2c/extensions --output-dir .
    Any explicit flag in extra_args overrides the default.
    """
    has_yaml = os.path.isfile("helmfile2compose.yaml")
    if not has_yaml:
        print("No helmfile2compose.yaml found — installing h2c-core only")
    _install(core_version=core_version, no_reinstall=no_reinstall, ignored=ignored)
    print()

    h2c = os.path.join(".h2c", CORE_FILE)
    cmd = [sys.executable, h2c]

    extensions_dir = os.path.join(".h2c", "extensions")
    if os.path.isdir(extensions_dir) and "--extensions-dir" not in extra_args:
        cmd += ["--extensions-dir", extensions_dir]
    if "--output-dir" not in extra_args:
        cmd += ["--output-dir", "."]
    if "--helmfile-dir" not in extra_args and "--from-dir" not in extra_args:
        cmd += ["--helmfile-dir", "."]

    cmd += extra_args
    sys.exit(subprocess.call(cmd))


# ---------------------------------------------------------------------------
# Info mode
# ---------------------------------------------------------------------------

def _info(names):
    """Display info about extensions from the registry.

    If *names* is empty, show all extensions. Otherwise show only the
    named ones (with dependency resolution — deps are included).
    """
    registry = _fetch_registry()

    if names:
        requested = [_parse_extension_arg(n) for n in names]
        resolved = _resolve_dependencies(requested, registry)
        show = [name for name, _, _ in resolved]
    else:
        show = sorted(registry)

    for name in show:
        entry = registry.get(name)
        if entry is None:
            print(f"{name}: unknown extension")
            print()
            continue

        print(f"{name}")
        print(f"  {entry.get('description', '(no description)')}")
        print(f"  repo: {entry['repo']}")

        tag = _latest_tag_safe(entry["repo"])
        if tag:
            print(f"  latest: {tag}")
        else:
            print("  latest: (could not fetch)")

        deps = entry.get("depends", [])
        if deps:
            print(f"  depends: {', '.join(deps)}")

        incompat = entry.get("incompatible", [])
        if incompat:
            print(f"  incompatible: {', '.join(incompat)}")

        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """CLI entry point. Dispatches to run mode or install mode."""
    # Intercept "run" before argparse — it has its own argument style
    args_before_run = []
    run_idx = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "run":
            run_idx = i
            break
        args_before_run.append(arg)
    if run_idx is not None:
        no_reinstall = "--no-reinstall" in args_before_run
        core_version = None
        ignored = set()
        for i, arg in enumerate(args_before_run):
            if arg == "--core-version" and i + 1 < len(args_before_run):
                core_version = args_before_run[i + 1]
            if arg == "--ignore-compatibility-errors":
                j = i + 1
                while j < len(args_before_run) and not args_before_run[j].startswith("-"):
                    ignored.add(args_before_run[j])
                    j += 1
        _run(sys.argv[run_idx + 1:], no_reinstall=no_reinstall,
             core_version=core_version, ignored=ignored or None)
        return

    parser = argparse.ArgumentParser(
        description="Download h2c-core and extensions.",
        epilog="Examples:\n"
               "  h2c-manager.py keycloak\n"
               "  h2c-manager.py --core-version v2.0.0 keycloak\n"
               "  h2c-manager.py keycloak==0.1.0\n"
               "  h2c-manager.py -d ./tools keycloak\n"
               "  h2c-manager.py run -e compose\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "extensions", nargs="*", metavar="EXTENSION",
        help="Extension to install (e.g. 'keycloak', 'keycloak==0.1.0')")
    parser.add_argument(
        "--core-version", default=None,
        help="Pin h2c-core to a specific version tag (e.g. v2.0.0)")
    parser.add_argument(
        "-d", "--dir", default=".h2c",
        help="Install directory (default: .h2c)")
    parser.add_argument(
        "--no-reinstall", action="store_true",
        help="Skip download and reuse cached install directory")
    parser.add_argument(
        "--ignore-compatibility-errors", nargs="+", metavar="EXT",
        default=[], help="Bypass incompatibility checks for these extensions")
    parser.add_argument(
        "--info", action="store_true",
        help="Show extension info instead of installing")
    args = parser.parse_args()

    if args.info:
        names = args.extensions
        if not names:
            yaml_depends, _ = _read_yaml_config()
            names = yaml_depends
        _info(names)
        return

    _install(
        core_version=args.core_version,
        extensions=args.extensions or None,
        install_dir=args.dir,
        no_reinstall=args.no_reinstall,
        ignored=set(args.ignore_compatibility_errors) or None,
    )


if __name__ == "__main__":
    main()
