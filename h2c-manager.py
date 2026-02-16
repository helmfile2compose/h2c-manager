#!/usr/bin/env python3
"""h2c-manager — lightweight package manager for helmfile2compose.

Downloads h2c-core (helmfile2compose.py) and optional extensions
from GitHub, resolving versions and dependencies automatically.

Usage:
    python3 h2c-manager.py                        # install core only
    python3 h2c-manager.py keycloak                # install core + keycloak operator
    python3 h2c-manager.py keycloak==0.1.0         # pin extension version
    python3 h2c-manager.py --core-version v2.0.0   # pin core version
    python3 h2c-manager.py -d ./tools keycloak     # custom install dir
    python3 h2c-manager.py run -e compose          # run h2c with smart defaults
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
            print(f"Error: file not found", file=sys.stderr)
            print(f"  URL: {url}", file=sys.stderr)
            sys.exit(1)
        raise


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


# ---------------------------------------------------------------------------
# Run mode
# ---------------------------------------------------------------------------

def _run(extra_args):
    """Run helmfile2compose.py with smart defaults.

    Defaults: --helmfile-dir . --extensions-dir .h2c/extensions --output-dir .
    Any explicit flag in extra_args overrides the default.
    """
    h2c = os.path.join(".h2c", CORE_FILE)
    if not os.path.isfile(h2c):
        print(f"Error: {h2c} not found. Run h2c-manager.py first to install.",
              file=sys.stderr)
        sys.exit(1)

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
# Main
# ---------------------------------------------------------------------------

def main():
    # Intercept "run" before argparse — it has its own argument style
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        _run(sys.argv[2:])
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
    args = parser.parse_args()

    install_dir = args.dir
    extensions_dir = os.path.join(install_dir, "extensions")

    # -- Core ---------------------------------------------------------------

    if args.core_version:
        core_tag = _normalize_tag(args.core_version)
    else:
        core_tag = _latest_tag(CORE_REPO)

    print(f"Fetching h2c-core {core_tag}...")
    core_url = _raw_url(CORE_REPO, core_tag, CORE_FILE)
    core_content = _download_or_die(core_url)

    # -- Extensions ---------------------------------------------------------

    requested = [_parse_extension_arg(ext) for ext in args.extensions]
    extension_files = []  # (name, local_name, content_bytes)
    extensions_with_reqs = []  # (name, [requirement_lines])

    if requested:
        registry = _fetch_registry()
        resolved = _resolve_dependencies(requested, registry)

        for name, pinned, is_dep in resolved:
            entry = registry[name]
            tag, version_display = _resolve_extension_version(pinned, entry)

            dep_of = ""
            if is_dep:
                # Find which requested extensions depend on this one
                dependents = []
                for req_name, _ in requested:
                    req_entry = registry.get(req_name, {})
                    if name in req_entry.get("depends", []):
                        dependents.append(req_name)
                if dependents:
                    dep_of = f" (dependency of {', '.join(dependents)})"

            print(f"Fetching extension {name} {version_display}{dep_of}...")

            file_path = entry["file"]
            file_url = _raw_url(entry["repo"], tag, file_path)
            content = _download_or_die(file_url)

            # Derive the local filename from the file field
            local_name = os.path.basename(file_path)
            extension_files.append((name, local_name, content))

            # Try to fetch requirements.txt (optional)
            reqs_url = _raw_url(entry["repo"], tag, "requirements.txt")
            reqs_data = _download(reqs_url)
            if reqs_data:
                req_lines = reqs_data.decode("utf-8").splitlines()
                extensions_with_reqs.append((name, req_lines))

    # -- Dependency check ---------------------------------------------------

    if extensions_with_reqs:
        missing = _check_requirements(extensions_with_reqs)
        if missing:
            print(file=sys.stderr)
            print("Missing Python dependencies for extensions:",
                  file=sys.stderr)
            for ext_name, req_line in missing:
                print(f"  {ext_name}: {req_line}", file=sys.stderr)
            all_reqs = sorted(set(r for _, r in missing))
            print(f"\nInstall with: pip install {' '.join(all_reqs)}",
                  file=sys.stderr)
            print(file=sys.stderr)

    # -- Write files --------------------------------------------------------

    core_path = os.path.join(install_dir, CORE_FILE)
    _write_file(core_path, core_content)
    print(f"Wrote {core_path}")

    for name, local_name, content in extension_files:
        ext_path = os.path.join(extensions_dir, local_name)
        _write_file(ext_path, content)
        print(f"Wrote {ext_path}")


if __name__ == "__main__":
    main()
