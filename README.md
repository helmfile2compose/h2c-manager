# h2c-manager

![vibe coded](https://img.shields.io/badge/vibe-coded-ff69b4)
![python 3](https://img.shields.io/badge/python-3-3776AB)
![heresy: 1/10](https://img.shields.io/badge/heresy-1%2F10-brightgreen)
![stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen)
![public domain](https://img.shields.io/badge/license-public%20domain-brightgreen)

Lightweight package manager for [helmfile2compose](https://github.com/helmfile2compose/h2c-core). Downloads the core script and CRD operator modules from GitHub releases. Python 3, stdlib only — no dependencies.

## Usage

```bash
# Download from main (rolling release)
curl -fsSL https://raw.githubusercontent.com/helmfile2compose/h2c-manager/main/h2c-manager.py -o h2c-manager.py

# Core only
python3 h2c-manager.py

# Core + operators (from CLI)
python3 h2c-manager.py keycloak certmanager trust-manager

# Core + operators (from helmfile2compose.yaml depends list)
python3 h2c-manager.py

# Pin versions
python3 h2c-manager.py --core-version v2.0.0 keycloak==0.1.0

# Custom install directory
python3 h2c-manager.py -d ./tools keycloak

# Run helmfile2compose with smart defaults
python3 h2c-manager.py run -e compose
```

## Run mode

`run` is a shortcut to invoke helmfile2compose with sensible defaults:

```bash
python3 h2c-manager.py run -e compose
# equivalent to:
# python3 .h2c/helmfile2compose.py --helmfile-dir . --extensions-dir .h2c/extensions --output-dir . -e compose
```

Defaults: `--helmfile-dir .`, `--extensions-dir .h2c/extensions` (if it exists), `--output-dir .`. Any explicit flag overrides the default. All extra arguments are passed through to helmfile2compose.

## Declarative dependencies

If `helmfile2compose.yaml` exists, h2c-manager reads `core_version` and `depends` from it:

```yaml
core_version: v2.0.0
depends:
  - keycloak
  - certmanager==0.1.0
  - trust-manager
```

```bash
python3 h2c-manager.py
# Core version from helmfile2compose.yaml: v2.0.0
# Reading extensions from helmfile2compose.yaml: keycloak, certmanager==0.1.0, trust-manager
```

CLI flags (`--core-version`, explicit extension args) override the yaml.

## Output

```
.h2c/
├── helmfile2compose.py
└── extensions/
    ├── keycloak.py
    └── certmanager.py        # auto-resolved as dep of trust-manager
```

## Extension registry

`extensions.json` maps extension names (operators, ingress rewriters, etc.) to GitHub repos. Available versions are whatever tags/releases exist on each repo — the registry doesn't list versions, GitHub is the source of truth.

### Schema

```json
{
  "schema_version": 1,
  "extensions": {
    "<name>": {
      "repo": "<org>/<repo>",
      "description": "human-readable description",
      "file": "<filename>.py",
      "depends": ["<other-extension>"]
    }
  }
}
```

### Adding an extension

Open a PR to this repo adding your extension to `extensions.json`. The repo must have at least one GitHub Release.

## Documentation

Full docs: [h2c-manager usage guide](https://helmfile2compose.github.io/h2c-docs/maintainer/h2c-manager/)

## License

Public domain.
