#!/usr/bin/env python3

import argparse
import os
import re
import subprocess
import sys
import yaml


class DeployKeyError(Exception):
    """Base exception for deploy key operations."""

    pass


class CommandNotFoundError(DeployKeyError):
    """Raised when a required command is not found."""

    pass


class FluxSecretError(DeployKeyError):
    """Raised when flux secret generation fails."""

    pass


class SecretDataError(DeployKeyError):
    """Raised when secret data extraction fails."""

    pass


class OnePasswordError(DeployKeyError):
    """Raised when 1Password operations fail."""

    pass


class ValidationError(DeployKeyError):
    """Raised when input validation fails."""

    pass


def validate_usecase(usecase: str) -> None:
    """Validate that usecase follows the required format: lowercase with underscores."""
    if not re.match(r"^[a-z][a-z0-9_]*$", usecase):
        raise ValidationError(
            f"Invalid usecase format: '{usecase}'. "
            "Usecase must be lowercase, start with a letter, and only contain letters, numbers, and underscores."
        )


def normalize_git_url(git_url: str) -> str:
    """Convert git URL to the required SSH format: ssh://git@<host>/<path>.git"""
    # Check if already in correct SSH format
    ssh_pattern = r"^ssh://[^@]+@[^/]+/.+\.git$"
    if re.match(ssh_pattern, git_url):
        return git_url

    # Convert shorthand format git@host:path.git to ssh://git@host/path.git
    shorthand_pattern = r"^([^@]+)@([^:]+):(.+)\.git$"
    match = re.match(shorthand_pattern, git_url)
    if match:
        user, host, path = match.groups()
        return f"ssh://{user}@{host}/{path}.git"

    # Check if it's missing .git extension
    ssh_no_git_pattern = r"^ssh://[^@]+@[^/]+/.+$"
    if re.match(ssh_no_git_pattern, git_url) and not git_url.endswith(".git"):
        return f"{git_url}.git"

    raise ValidationError(
        f"Invalid git URL format: '{git_url}'. "
        "Expected format: ssh://user@host/path.git or user@host:path.git"
    )


def check_required_commands(required_commands: list[str]) -> None:
    """Check if required external commands are available."""
    for cmd in required_commands:
        try:
            subprocess.run(["which", cmd], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            raise CommandNotFoundError(f"{cmd} command not found")


def flux_secret_with_key(git_url: str, key_content: str) -> str:
    """Generate flux secret using provided key content and return the YAML content as string."""
    try:
        # Use stdin to pass the key content to flux
        result = subprocess.run(
            [
                "flux",
                "create",
                "secret",
                "git",
                "setup-deploy-key",
                "--export",
                "--private-key-file=-",  # Read from stdin
                f"--url={git_url}",
            ],
            input=key_content,
            check=True,
            capture_output=True,
            text=True,
        )

        return result.stdout

    except subprocess.CalledProcessError as e:
        raise FluxSecretError(f"Failed to generate flux secret: {e.stderr}")


def flux_secret_with_generated_key(git_url: str) -> str:
    """Generate flux secret with flux-generated key and return the YAML content as string."""
    try:
        result = subprocess.run(
            [
                "flux",
                "create",
                "secret",
                "git",
                "setup-deploy-key",
                "--export",
                f"--url={git_url}",
                "--ssh-key-algorithm=ecdsa",
                "--ssh-ecdsa-curve=p521",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        return result.stdout

    except subprocess.CalledProcessError as e:
        raise FluxSecretError(f"Failed to generate flux secret: {e.stderr}")


def extract_secret_data(yaml_content: str) -> tuple[str, str, str]:
    """Extract identity, identity.pub, and known_hosts from the flux secret YAML."""
    try:
        # Parse YAML content
        secret_data = yaml.safe_load(yaml_content)

        # Extract stringData fields
        string_data = secret_data.get("stringData", {})
        identity = string_data.get("identity")
        identity_pub = string_data.get("identity.pub")
        known_hosts = string_data.get("known_hosts")

        # Validate extracted data
        if not identity or not identity_pub or not known_hosts:
            raise SecretDataError(
                f"Failed to extract required fields from flux output. Generated flux output:\n{yaml_content}"
            )

        return identity, identity_pub, known_hosts

    except yaml.YAMLError as e:
        raise SecretDataError(f"Failed to parse YAML: {e}")
    except SecretDataError:
        raise
    except Exception as e:
        raise SecretDataError(f"Failed to extract secret data: {e}")


def check_1password_item_exists(item_name: str, vault_name: str) -> bool:
    """Check if a 1Password item already exists."""
    try:
        subprocess.run(
            ["op", "item", "get", item_name, f"--vault={vault_name}"],
            check=True,
            capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def create_1password_item(
    item_name: str,
    vault_name: str,
    identity: str,
    identity_pub: str,
    known_hosts: str,
    usecase: str,
    git_url: str,
) -> None:
    """Create a 1Password item with the secret data."""
    try:
        subprocess.run(
            [
                "op",
                "item",
                "create",
                f"--vault={vault_name}",
                "--category=Secure Note",
                f"--title={item_name}",
                f"identity[password]={identity}",
                f"identity\.pub[text]={identity_pub}",
                f"known_hosts[text]={known_hosts}",
                f"repo_url[text]={git_url}",
                f"usecase_name[text]={usecase}",
            ],
            check=True,
            capture_output=True,
        )

    except subprocess.CalledProcessError as e:
        raise OnePasswordError(f"Failed to create 1Password item: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inject deploy keys into 1Password for Flux GitOps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s homelab-data ssh://git@github.com/user/repo.git --key-file ./key.pem
  %(prog)s homelab-data ssh://git@github.com/user/repo.git --generate-key
        """,
    )

    parser.add_argument("usecase", help="Use case name for the deploy key")
    parser.add_argument("git_url", help="Git repository URL")

    key_group = parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument("--key-file", help="Path to existing private key file")
    key_group.add_argument(
        "--generate-key",
        action="store_true",
        help="Generate a new ECDSA P-521 key using flux",
    )

    parser.add_argument(
        "--vault-name",
        default="homelab-k8s",
        help="1Password vault name (default: homelab-k8s)",
    )

    args = parser.parse_args()

    # Validate inputs
    if not args.usecase or not args.git_url:
        print("Error: usecase and git_url are required", file=sys.stderr)
        sys.exit(1)

    try:
        # Validate usecase format
        validate_usecase(args.usecase)

        # Normalize git URL to required format
        args.git_url = normalize_git_url(args.git_url)

    except ValidationError as e:
        print(f"Validation Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Check required commands
        check_required_commands(["flux", "op"])

        # Generate flux secret
        if args.generate_key:
            print("Generating new ECDSA P-521 key using flux...")
            yaml_content = flux_secret_with_generated_key(args.git_url)
        else:
            if not os.path.isfile(args.key_file):
                print(
                    f"Error: Private key file {args.key_file} does not exist",
                    file=sys.stderr,
                )
                sys.exit(1)

            with open(args.key_file, "r") as f:
                key_content = f.read()

            yaml_content = flux_secret_with_key(args.git_url, key_content)

        # Extract secret data
        identity, identity_pub, known_hosts = extract_secret_data(yaml_content)

        # Create 1Password item
        item_name = f"deploy_key_{args.usecase}"

        if check_1password_item_exists(item_name, args.vault_name):
            print(
                f"Error: 1Password item '{item_name}' already exists in vault '{args.vault_name}'",
                file=sys.stderr,
            )
            print(
                "Please delete the existing item or use a different usecase name",
                file=sys.stderr,
            )
            sys.exit(1)

        create_1password_item(
            item_name,
            args.vault_name,
            identity,
            identity_pub,
            known_hosts,
            args.usecase,
            args.git_url,
        )

        print(
            f"Successfully created 1Password item: {item_name} in vault: {args.vault_name}"
        )

    except DeployKeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
