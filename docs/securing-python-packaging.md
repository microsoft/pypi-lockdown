Configuring Python package managers to install from an Azure DevOps Artifacts feed using [`artifacts-keyring-nofuss`](https://github.com/microsoft/artifacts-keyring-nofuss) and [`pypi-lockdown`](https://github.com/microsoft/pypi-lockdown) — pure-Python, no .NET, and automation-friendly (`pypi-lockdown` writes user-global config by default and never prompts; opt into project-level edits with `--project`).

## Contents

- [Setup](#setup)
  - [Option 1: uv (recommended)](#option-1-uv-recommended)
  - [Option 2: pip / conda](#option-2-pip--conda)
  - [Install uv (hash-verified)](#install-uv-hash-verified)
- [How authentication works](#how-authentication-works)
- [Scenarios](#scenarios)
  - [Local development](#local-development-windows-macos-linux-wsl)
  - [ADO pipeline (uv)](#ado-pipeline-uv)
  - [ADO pipeline (pip)](#ado-pipeline-pip)
  - [GitHub Actions — OIDC](#github-actions--oidc-workload-identity-federation)
  - [GitHub Actions — self-hosted runner](#github-actions--self-hosted-runner-with-managed-identity)
  - [Docker build](#docker-build)
  - [VS Code devcontainer / Codespaces](#vs-code-devcontainer--github-codespaces)
- [Debugging](#debugging)

---

Set `$PRIVATE_FEED` to your team's feed URL and `$PUBLIC_FEED` to the public
bootstrap feed that hosts `pypi-lockdown` + the keyring backends, e.g.:

```bash
PRIVATE_FEED="https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/"
PUBLIC_FEED="https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
```

## Install the keyring backend (once)

uv and pip authenticate to the private feed by invoking a global `keyring`
command, so install it once as a tool with an Azure Artifacts backend — from the
**public** feed (the backends are hosted there, not on the private feed):

```bash
# pure-Python fork (no .NET, automation-friendly)
uv tool install keyring --with artifacts-keyring-nofuss --index-url "$PUBLIC_FEED"
# ...or the upstream backend (requires .NET):
uv tool install keyring --with artifacts-keyring     --index-url "$PUBLIC_FEED"
```

(`pipx` works too: `pipx install keyring --index-url "$PUBLIC_FEED" && pipx
inject keyring artifacts-keyring-nofuss --index-url "$PUBLIC_FEED"`.)

# Setup

## Option 1: uv (recommended)

### pyproject.toml

```toml
[tool.uv]
keyring-provider = "subprocess"

[[tool.uv.index]]
name = "ado-feed"
url = "https://__token__@pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/"
authenticate = "always"
default = true
```

### Install and configure (using pypi-lockdown)

```bash
pip install pypi-lockdown --index-url "$PUBLIC_FEED"
pypi-lockdown "$PRIVATE_FEED"
```

This writes **user-global** pip + uv config (with `keyring-provider = subprocess`)
pointing at your feed — so every environment authenticates via the global
`keyring` tool installed above.  Global config already covers pip, uv, and
Hatch; pass `--project` to *also* write `[tool.uv]` + `[[tool.uv.index]]` (and
Poetry/Hatch) config directly into `./pyproject.toml`.

> **Inspect or roll back:** run `pypi-lockdown status` to see which pip/uv/project
> files are configured (and which are managed by pypi-lockdown), or
> `pypi-lockdown undo` to remove the managed global config again (add `--project`
> to also clean `./pyproject.toml`).

### Alternative: manual setup

Skip `pypi-lockdown` and add the `pyproject.toml` section above (plus a
user-level `uv.toml`) by hand.

### Usage

```bash
uv lock          # resolve deps → uv.lock (commit this)
uv sync --locked # install from uv.lock
```

## Option 2: pip / conda

### One-time setup

```bash
pip install pypi-lockdown --index-url "$PUBLIC_FEED"
pypi-lockdown "$PRIVATE_FEED"
```

This writes user-global `pip.conf` with `keyring-provider = subprocess`, so all
future `pip install` commands (in any environment, conda included) authenticate
automatically via the global `keyring` tool.

> **Lock down a single environment instead?** Activate it and run
> `pypi-lockdown --env "$PRIVATE_FEED"`.  That writes `pip.conf` into the active
> environment and copies a backend into it, so first install
> `pip install "pypi-lockdown[nofuss]" --index-url "$PUBLIC_FEED"` (or
> `[official]`).

### Usage

```bash
pip install <package>          # resolves from the configured feed
pip install -r requirements.txt
```

### Team onboarding shortcut

If your project already has a `pyproject.toml` with the feed URL configured, team members can simply run:

```bash
pip install pypi-lockdown --index-url "$PUBLIC_FEED"
pypi-lockdown        # auto-detects feed URL from pyproject.toml
```

## Install uv (hash-verified)

### Windows (PowerShell)

```powershell
$v="0.10.12";$h="688FB18494B49A651726C3830060AAE8F2B1B84864B66B0CFDFBBAE93E72A38F";$f="$env:TEMP\uv-install.ps1"
irm "https://astral.sh/uv/$v/install.ps1" -OutFile $f
if((Get-FileHash $f SHA256).Hash-ne$h){rm $f;throw "Hash mismatch!"}
& $f;rm $f
```

### Linux / WSL / macOS

```bash
V="0.10.12"; H="2dbc8204431a43a30f5396f3bb94d3f4505a2aabd4d35a9f75d5d9d6cfa81528"; F=$(mktemp)
trap 'rm -f "$F"' EXIT
curl -fsSL "https://astral.sh/uv/$V/install.sh" -o "$F"
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL=$(sha256sum "$F" | awk '{print $1}')
else
  ACTUAL=$(shasum -a 256 "$F" | awk '{print $1}')
fi
if [ "$ACTUAL" != "$H" ]; then echo "Hash mismatch!" >&2; exit 1; fi
sh "$F"
```

# How authentication works

The backend tries providers in order and uses the first that succeeds:

| # | Provider | Env vars | Best for |
|---|---|---|---|
| 1 | **Env var** | `ARTIFACTS_KEYRING_NOFUSS_TOKEN` (or `VSS_NUGET_ACCESSTOKEN`) | CI pipelines, Docker builds |
| 2 | **Azure CLI** | _(none — uses `az` login session)_ | Local development |
| 3 | **Workload Identity** | `AZURE_CLIENT_ID` + `AZURE_FEDERATED_TOKEN_FILE` + `AZURE_TENANT_ID` | GitHub Actions with `azure/login@v2` |
| 4 | **Managed Identity** | `AZURE_CLIENT_ID` _(optional, for user-assigned)_ | Azure VMs, self-hosted runners |

For user tokens (Azure CLI), the bearer token is exchanged for a scoped, read-only session token (`vso.packaging`).  For service principal tokens (Workload Identity, Managed Identity), the bearer token is used directly without exchange.

# Scenarios

## Local development (Windows, macOS, Linux, WSL)

Just log in to Azure CLI once — everything else is automatic:

```bash
az login
uv sync          # or: pip install <package>
```

> ⚠️ **WSL:** use `uv sync --no-progress` to avoid slow progress-bar rendering that can trigger ADO's DDoS protection.

## ADO pipeline (uv)

uv is **not** served from the feed — install it out-of-band first (see
[Install uv (hash-verified)](#install-uv-hash-verified)), then bootstrap the
keyring backend from the feed:

```yaml
steps:
  - script: |
      # install uv out-of-band; pin + hash-verify per "Install uv (hash-verified)"
      curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
    displayName: Install uv
    env:
      UV_VERSION: "0.10.12"
  - script: |
      pip install keyring artifacts-keyring-nofuss \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      uv sync --locked
    env:
      UV_KEYRING_PROVIDER: subprocess
      ARTIFACTS_KEYRING_NOFUSS_TOKEN: $(System.AccessToken)
```

## ADO pipeline (pip)

```yaml
steps:
  - script: |
      pip install pypi-lockdown \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      python -m pypi_lockdown --ci "$PRIVATE_FEED"
      pip install -r requirements.txt
    env:
      ARTIFACTS_KEYRING_NOFUSS_TOKEN: $(System.AccessToken)
      PRIVATE_FEED: https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/
```

## GitHub Actions — OIDC (Workload Identity Federation)

The `azure/login@v2` action sets the env vars that the workload-identity provider needs — no token-passing required:

```yaml
steps:
  - uses: azure/login@v2
    with:
      client-id: ${{ secrets.AZURE_CLIENT_ID }}
      tenant-id: ${{ secrets.AZURE_TENANT_ID }}
      allow-no-subscriptions: true

  # uv is NOT served from the feed — install it out-of-band.
  - uses: astral-sh/setup-uv@v6
    with:
      version: "0.10.12"

  - run: |
      pip install keyring artifacts-keyring-nofuss \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      uv sync --locked
    env:
      UV_KEYRING_PROVIDER: subprocess
```

See also: [GitHub OIDC setup](https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust?pivots=identity-wif-apps-methods-azp#github-actions) for configuring the App Registration and federated credentials.

## GitHub Actions — self-hosted runner with Managed Identity

If the runner has a managed identity with access to the ADO feed, authentication is fully automatic:

```yaml
steps:
  # uv is NOT served from the feed — install it out-of-band.
  - uses: astral-sh/setup-uv@v6
    with:
      version: "0.10.12"

  - run: |
      pip install keyring artifacts-keyring-nofuss \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      uv sync --locked
    env:
      UV_KEYRING_PROVIDER: subprocess
      # Set AZURE_CLIENT_ID if using a user-assigned managed identity
```

See also: [GitHub self-hosted runners documentation](https://docs.github.com/en/actions/hosting-your-own-runners) for provisioning the runner and granting its managed identity access to the ADO feed.

## Docker build

Obtain a bearer token, then pass it as a build secret:

```bash
# Local: mint a token from Azure CLI
ACCESS_TOKEN=$(az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv)

# ADO pipeline: use $(System.AccessToken)
# GitHub Actions: use the token from azure/login or managed identity

docker buildx build --secret id=ACCESS_TOKEN,env=ACCESS_TOKEN .
```

```dockerfile
RUN --mount=type=secret,id=ACCESS_TOKEN,env=ARTIFACTS_KEYRING_NOFUSS_TOKEN \
    uv sync --locked
```

For pip-based Dockerfiles:

```dockerfile
RUN --mount=type=secret,id=ACCESS_TOKEN,env=ARTIFACTS_KEYRING_NOFUSS_TOKEN \
    pip install -r requirements.txt
```

(Requires `keyring` + `artifacts-keyring-nofuss` installed earlier in the image.)

## VS Code devcontainer / GitHub Codespaces

```json
{
  "features": {
    "ghcr.io/devcontainers/features/azure-cli:1": {}
  },
  "containerEnv": {
    "UV_KEYRING_PROVIDER": "subprocess"
  },
  "postCreateCommand": "uv tool install keyring --with artifacts-keyring-nofuss --index-url https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
}
```

After the container starts, `az login` once (VS Code tunnels the browser).  Then `uv sync` works.  In CI, pass a token via `ARTIFACTS_KEYRING_NOFUSS_TOKEN` instead.

# Debugging

```bash
export ARTIFACTS_KEYRING_NOFUSS_DEBUG=1
uv sync   # debug output goes to stderr
pip install <package>  # same — debug output on stderr
```

See also: [artifacts-keyring-nofuss README](https://github.com/microsoft/artifacts-keyring-nofuss) for advanced troubleshooting.
