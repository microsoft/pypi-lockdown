Configuring Python package managers to install from an Azure DevOps Artifacts feed using [`artifacts-keyring-nofuss`](https://github.com/microsoft/artifacts-keyring-nofuss) and [`pypi-lockdown`](https://github.com/microsoft/pypi-lockdown) — pure-Python, no .NET, no interactive prompts.

Set `$PRIVATE_FEED` to your team's feed URL, e.g.:

```bash
PRIVATE_FEED="https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/"
```

# Setup

## Option 1: uv (recommended)

### pyproject.toml

```toml
[tool.uv]
keyring-provider = "subprocess"

[[tool.uv.index]]
name = "ado-feed"
url = "https://VssSessionToken@$PRIVATE_FEED"
authenticate = "always"
default = true
```

### Install and configure (using pypi-lockdown)

```bash
pip install pypi-lockdown \
  --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
python -m pypi_lockdown "$PRIVATE_FEED"
```

This writes the `[tool.uv]` + `[[tool.uv.index]]` config into `pyproject.toml`, installs `keyring` + `artifacts-keyring-nofuss`, and configures pip — all in one step.

### Alternative: manual setup

```bash
uv tool install keyring --with artifacts-keyring-nofuss \
  --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
```

Then add the `pyproject.toml` section above by hand.

### Usage

```bash
uv lock          # resolve deps → uv.lock (commit this)
uv sync --locked # install from uv.lock
```

## Option 2: pip / conda

### One-time environment setup

```bash
# Activate your environment (venv, conda, etc.)
pip install pypi-lockdown \
  --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"

python -m pypi_lockdown "$PRIVATE_FEED"
```

This writes `pip.conf` (scoped to the active environment) and installs `keyring` + `artifacts-keyring-nofuss`.  All future `pip install` commands authenticate automatically.

For conda environments, run the same commands after `conda activate`.

### Usage

```bash
pip install <package>          # resolves from the configured feed
pip install -r requirements.txt
```

### Team onboarding shortcut

If your project already has a `pyproject.toml` with the feed URL configured, team members can simply run:

```bash
pip install pypi-lockdown \
  --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
python -m pypi_lockdown        # auto-detects feed URL from pyproject.toml
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
curl -fsSL "https://astral.sh/uv/$V/install.sh" -o "$F"
echo "$H  $F" | sha256sum -c - && sh "$F" || echo "Hash mismatch!"
rm "$F"
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

```yaml
steps:
  - script: |
      pip install uv keyring artifacts-keyring-nofuss \
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

  - run: |
      pip install uv keyring artifacts-keyring-nofuss \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      uv sync --locked
    env:
      UV_KEYRING_PROVIDER: subprocess
```

See also: [C(i): GitHub OIDC setup](/Getting-started/Securing-uv/C\(i\):-GitHub) for configuring the App Registration and federated credentials.

## GitHub Actions — self-hosted runner with Managed Identity

If the runner has a managed identity with access to the ADO feed, authentication is fully automatic:

```yaml
steps:
  - run: |
      pip install uv keyring artifacts-keyring-nofuss \
        --index-url "https://pkgs.dev.azure.com/pypi-lockdown/pypi-lockdown/_packaging/public@Local/pypi/simple/"
      uv sync --locked
    env:
      UV_KEYRING_PROVIDER: subprocess
      # Set AZURE_CLIENT_ID if using a user-assigned managed identity
```

GIM policy prefers self-hosted runners over GitHub-hosted runners ([source](https://eng.ms/docs/more/github-inside-microsoft/policies/actions)).

See also: [C(ii): GitHub self-hosted runner](/Getting-started/Securing-uv/C\(ii\):-GitHub-self%2Dhosted-runner) for provisioning the runner and granting its managed identity access to the ADO feed.

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

See also: [Accessing Package Feed - CATS](https://super-adventure-v7nqwml.pages.github.io/researcher_documentation/cfs_access/)
