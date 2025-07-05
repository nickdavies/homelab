# Setup Deploy Key

A Python tool to inject deploy keys into 1Password for Flux GitOps.

## Installation

Install the required dependencies:

```bash
pip install -r requirements.txt
```

## Usage

The tool supports two modes:

1. **Using an existing key file:**
```bash
python setup_deploy_key.py homelab-data ssh://git@github.com/user/repo.git --key-file ./key.pem
```

2. **Generating a new ed25519 key:**
```bash
python setup_deploy_key.py homelab-data ssh://git@github.com/user/repo.git --generate-key
```

## Options

- `usecase`: Use case name for the deploy key
- `git_url`: Git repository URL
- `--key-file`: Path to existing private key file
- `--generate-key`: Generate a new ed25519 key
- `--vault-name`: 1Password vault name (default: homelab-k8s)

## Requirements

The following external commands must be available:
- `flux`
- `op` (1Password CLI)
