#!/usr/bin/env python3
"""dekube-manager — lightweight package manager for helmfile2compose.

Downloads a dekube distribution and optional extensions
from GitHub, resolving versions and dependencies automatically.

Usage:
    python3 dekube-manager.py                              # install distribution (+ depends from yaml)
    python3 dekube-manager.py keycloak                      # install distribution + keycloak operator
    python3 dekube-manager.py keycloak==0.1.0               # pin extension version
    python3 dekube-manager.py --distribution-version v2.0.0 # pin distribution version
    python3 dekube-manager.py --distribution engine           # use bare engine instead of full distribution
    python3 dekube-manager.py --no-distribution keycloak     # extensions only (no distribution)
    python3 dekube-manager.py -d ./tools keycloak            # custom install dir
    python3 dekube-manager.py --no-reinstall                 # skip download, use cached .dekube/
    python3 dekube-manager.py --info                         # show info for all extensions (or yaml depends)
    python3 dekube-manager.py --info nginx traefik           # show info for specific extensions
    python3 dekube-manager.py run -e compose                 # run dekube with smart defaults

By default, the distribution and extensions are always re-downloaded
(overwriting any cached files). Use --no-reinstall to skip the download
and reuse the existing .dekube/ directory.

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

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required but not installed", file=sys.stderr)
    print("  Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_DISTRIBUTION = "helmfile2compose"
REGISTRY_URL = "https://manager.dekube.io/extensions.json"
DISTRIBUTIONS_URL = "https://manager.dekube.io/distributions.json"
GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _github_get(url):
    """GET a GitHub API or raw URL, return bytes. Raises on HTTP error."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "dekube-manager",
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
# dekube.yaml — depends resolution
# ---------------------------------------------------------------------------

def _read_yaml_config(yaml_path=None):
    """Read 'depends', 'distribution', and 'distribution_version' from yaml.

    Returns (depends_list, distribution_or_none, distribution_version_or_none).
    Backwards compat: 'core_version' is read as fallback for 'distribution_version'.
    """
    if yaml_path is None:
        if os.path.isfile("dekube.yaml"):
            yaml_path = "dekube.yaml"
        elif os.path.isfile("helmfile2compose.yaml"):
            print("Found helmfile2compose.yaml but no dekube.yaml — reading it, "
                  "but consider renaming it", file=sys.stderr)
            yaml_path = "helmfile2compose.yaml"
        else:
            return [], None, None
    if not os.path.isfile(yaml_path):
        return [], None, None

    with open(yaml_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    depends = doc.get("depends") or []
    distribution = doc.get("distribution") or None
    distribution_version = doc.get("distribution_version") or None
    if not distribution_version:
        distribution_version = doc.get("core_version") or None
    return depends, distribution, distribution_version


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
# Distribution registry
# ---------------------------------------------------------------------------

def _fetch_distributions():
    """Fetch and parse the distributions.json registry."""
    try:
        data = _github_get(DISTRIBUTIONS_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print("Error: distributions registry not found", file=sys.stderr)
            print(f"  URL: {DISTRIBUTIONS_URL}", file=sys.stderr)
            sys.exit(1)
        raise
    registry = json.loads(data)
    return registry.get("distributions", {})


def _resolve_distribution(name, distributions):
    """Resolve a distribution name to (repo, file). Exits on unknown name."""
    entry = distributions.get(name)
    if entry is None:
        print(f"Error: unknown distribution '{name}'", file=sys.stderr)
        print(f"  Available: {', '.join(sorted(distributions))}",
              file=sys.stderr)
        sys.exit(1)
    return entry["repo"], entry["file"]


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


def _install_distribution(install_dir, dist_name, dist_version,
                          yaml_dist_version, no_reinstall):
    """Download a distribution into install_dir. Returns the filename.

    Skips download if cached and no_reinstall.
    """
    distributions = _fetch_distributions()
    repo, filename = _resolve_distribution(dist_name, distributions)

    dist_path = os.path.join(install_dir, filename)
    if no_reinstall and os.path.isfile(dist_path):
        print(f"Cached {dist_path}")
        return filename

    if dist_version:
        tag = _normalize_tag(dist_version)
    elif yaml_dist_version:
        tag = _normalize_tag(yaml_dist_version)
        print(f"Distribution version from dekube.yaml: {tag}")
    else:
        tag = _latest_tag(repo)

    url = _release_asset_url(repo, tag, filename)
    _fetch_file(url, dist_path, f"{dist_name} {tag}")
    return filename


def _validate_extensions(requested, ignored=None):
    """Fetch registry, resolve dependencies, return (registry, resolved).

    Exits with an error if any extension is unknown or incompatible.
    Call this before downloading the distribution so we fail fast on bad input.
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


def _install(distribution_version=None, distribution=None, extensions=None,
             install_dir=".dekube", no_reinstall=False, no_distribution=False,
             ignored=None):
    """Install a distribution and optional extensions.

    If distribution_version/extensions are not given, reads from
    dekube.yaml.

    Returns the distribution filename (e.g. 'helmfile2compose.py') or None
    when --no-distribution is used.

    When no_reinstall is True, existing files are kept as-is and only missing
    files are downloaded. There is no version tracking: a cached file is never
    updated unless you run without --no-reinstall (the default).
    """
    yaml_depends, yaml_distribution, yaml_dist_version = _read_yaml_config()

    ext_args = extensions if extensions is not None else []
    if not ext_args and yaml_depends:
        print(f"Reading extensions from dekube.yaml: "
              f"{', '.join(yaml_depends)}")
        ext_args = yaml_depends

    requested = [_parse_extension_arg(ext) for ext in ext_args]
    registry, resolved = _validate_extensions(requested, ignored=ignored)

    dist_file = None
    if not no_distribution:
        dist_name = distribution or yaml_distribution or DEFAULT_DISTRIBUTION
        dist_file = _install_distribution(
            install_dir, dist_name, distribution_version, yaml_dist_version,
            no_reinstall)

    extensions_dir = os.path.join(install_dir, "extensions")
    extensions_with_reqs = _install_extensions(
        extensions_dir, registry, resolved, requested, no_reinstall)

    missing = _check_requirements(extensions_with_reqs)
    if missing:
        print(file=sys.stderr)
        print("Missing Python dependencies:", file=sys.stderr)
        for component, req_line in missing:
            print(f"  {component}: {req_line}", file=sys.stderr)
        all_reqs = sorted(set(r for _, r in missing))
        print(f"\nInstall with: pip install {' '.join(all_reqs)}",
              file=sys.stderr)
        print(file=sys.stderr)

    return dist_file


def _run(extra_args, no_reinstall=False, distribution_version=None,
         distribution=None, ignored=None):
    """Run the distribution script with smart defaults.

    Downloads the distribution (+ extensions from dekube.yaml)
    before every run, overwriting cached files. Use --no-reinstall to skip
    already-cached files (missing files are still downloaded).
    Defaults: --helmfile-dir . --extensions-dir .dekube/extensions --output-dir .
    Any explicit flag in extra_args overrides the default.
    """
    has_yaml = os.path.isfile("dekube.yaml") or os.path.isfile("helmfile2compose.yaml")
    if not has_yaml:
        print("No dekube.yaml found — installing distribution only")
    dist_file = _install(distribution_version=distribution_version,
                         distribution=distribution, no_reinstall=no_reinstall,
                         ignored=ignored)
    print()

    if dist_file is None:
        print("Error: run mode requires a distribution", file=sys.stderr)
        sys.exit(1)

    h2c = os.path.join(".dekube", dist_file)
    cmd = [sys.executable, h2c]

    extensions_dir = os.path.join(".dekube", "extensions")
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
        distribution_version = None
        distribution = None
        ignored = set()
        for i, arg in enumerate(args_before_run):
            if arg == "--distribution-version" and i + 1 < len(args_before_run):
                distribution_version = args_before_run[i + 1]
            if arg == "--distribution" and i + 1 < len(args_before_run):
                distribution = args_before_run[i + 1]
            if arg == "--ignore-compatibility-errors":
                j = i + 1
                while j < len(args_before_run) and not args_before_run[j].startswith("-"):
                    ignored.add(args_before_run[j])
                    j += 1
        _run(sys.argv[run_idx + 1:], no_reinstall=no_reinstall,
             distribution_version=distribution_version,
             distribution=distribution, ignored=ignored or None)
        return

    parser = argparse.ArgumentParser(
        description="Download a dekube distribution and extensions.",
        epilog="Examples:\n"
               "  dekube-manager.py keycloak\n"
               "  dekube-manager.py --distribution-version v2.0.0 keycloak\n"
               "  dekube-manager.py --distribution engine\n"
               "  dekube-manager.py --no-distribution keycloak\n"
               "  dekube-manager.py keycloak==0.1.0\n"
               "  dekube-manager.py -d ./tools keycloak\n"
               "  dekube-manager.py run -e compose\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "extensions", nargs="*", metavar="EXTENSION",
        help="Extension to install (e.g. 'keycloak', 'keycloak==0.1.0')")
    parser.add_argument(
        "--distribution-version", default=None,
        help="Pin distribution to a specific version tag (e.g. v2.0.0)")
    parser.add_argument(
        "--distribution", default=None,
        help="Distribution to install (default: helmfile2compose)")
    parser.add_argument(
        "--no-distribution", action="store_true",
        help="Skip distribution download (extensions only)")
    parser.add_argument(
        "-d", "--dir", default=".dekube",
        help="Install directory (default: .dekube)")
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
            yaml_depends, _, _ = _read_yaml_config()
            names = yaml_depends
        _info(names)
        return

    _install(
        distribution_version=args.distribution_version,
        distribution=args.distribution,
        extensions=args.extensions or None,
        install_dir=args.dir,
        no_reinstall=args.no_reinstall,
        no_distribution=args.no_distribution,
        ignored=set(args.ignore_compatibility_errors) or None,
    )


if __name__ == "__main__":
    main()
