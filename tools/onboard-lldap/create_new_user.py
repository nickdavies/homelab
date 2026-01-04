"""
lldap User Onboarding Script
Creates a new user in lldap and sends them an onboarding email
"""

import requests
import secrets
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import argparse
import sys
import os
from dataclasses import dataclass


@dataclass
class Config:
    """Configuration for the onboarding script"""

    lldap_url: str
    lldap_admin_user: str
    lldap_admin_password: str
    smtp_host: str
    smtp_port: int
    email_from: str
    authelia_url: str

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables with validation"""
        missing: list[str] = []

        # Required variables
        required = {
            "LLDAP_URL": os.getenv("LLDAP_URL"),
            "LLDAP_ADMIN_USER": os.getenv("LLDAP_ADMIN_USER"),
            "LLDAP_ADMIN_PASSWORD": os.getenv("LLDAP_ADMIN_PASSWORD"),
            "SMTP_HOST": os.getenv("SMTP_HOST"),
            "EMAIL_FROM": os.getenv("EMAIL_FROM"),
            "AUTHELIA_URL": os.getenv("AUTHELIA_URL"),
        }

        for key, value in required.items():
            if not value:
                missing.append(key)

        if missing:
            print(
                f"Error: Missing required environment variables: {', '.join(missing)}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Parse SMTP port with default
        try:
            smtp_port = int(os.getenv("SMTP_PORT", "25"))
        except ValueError:
            print("Error: SMTP_PORT must be a valid integer", file=sys.stderr)
            sys.exit(1)

        return cls(
            lldap_url=required["LLDAP_URL"],
            lldap_admin_user=required["LLDAP_ADMIN_USER"],
            lldap_admin_password=required["LLDAP_ADMIN_PASSWORD"],
            smtp_host=required["SMTP_HOST"],
            smtp_port=smtp_port,
            email_from=required["EMAIL_FROM"],
            authelia_url=required["AUTHELIA_URL"],
        )


def get_lldap_token(config: Config) -> str:
    """Authenticate with lldap and get JWT token"""
    response = requests.post(
        f"{config.lldap_url}/auth/simple/login",
        headers={"Content-Type": "application/json"},
        json={
            "username": config.lldap_admin_user,
            "password": config.lldap_admin_password,
        },
    )

    if response.status_code != 200:
        raise Exception(
            f"Authentication failed ({response.status_code}): {response.text}"
        )

    data = response.json()
    return data["token"]


def create_user(
    config: Config,
    token: str,
    username: str,
    email: str,
    first_name: str,
    last_name: str,
    display_name: str,
) -> dict:
    """Create a new user in lldap"""
    query = """
    mutation CreateUser($user: CreateUserInput!) {
        createUser(user: $user) {
            id
            email
        }
    }
    """

    response = requests.post(
        f"{config.lldap_url}/api/graphql",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "query": query,
            "variables": {
                "user": {
                    "id": username,
                    "email": email,
                    "displayName": display_name,
                    "attributes": [
                        {"name": "first_name", "value": [first_name]},
                        {"name": "last_name", "value": [last_name]},
                    ],
                }
            },
        },
    )

    if response.status_code != 200:
        raise Exception(f"User creation failed: {response.text}")

    data = response.json()
    if "errors" in data:
        raise Exception(f"GraphQL error: {data['errors']}")

    return data["data"]["createUser"]


def send_onboarding_email(config: Config, email: str, username: str, display_name: str):
    """Send onboarding email with password reset link"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Welcome - Set Up Your Account"
    msg["From"] = config.email_from
    msg["To"] = email

    # Plain text version
    text = f"""
Hi {display_name},

Your account has been created! Your username is '{username}'. To get started, please set your password by visiting:

{config.authelia_url}

Click on "Reset password" and enter your email address to receive a password reset link.

If you have any questions, please contact your administrator.
    """

    # HTML version
    html = f"""
<html>
  <body>
    <p>Hi {display_name},</p>

    <p>Your account has been created! Your username is '{username}'. To get started, please set your password by visiting:</p>

    <p><a href="{config.authelia_url}">{config.authelia_url}</a></p>

    <p>Click on <strong>"Reset password"</strong> and enter your email address to receive a password reset link.</p>

    <p>If you have any questions, please contact your administrator.</p>
  </body>
</html>
    """

    part1 = MIMEText(text, "plain")
    part2 = MIMEText(html, "html")

    msg.attach(part1)
    msg.attach(part2)

    # Send email
    with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
        server.send_message(msg)


def main():
    parser = argparse.ArgumentParser(
        description="Create lldap user and send onboarding email"
    )
    parser.add_argument("--username", required=True, help="Username for the new user")
    parser.add_argument("--email", required=True, help="Email address for the new user")
    parser.add_argument("--first-name", required=True, help="Users first name")
    parser.add_argument("--last-name", required=True, help="Users last name")
    parser.add_argument(
        "--display-name",
        help="Display name (if different from First + Last name)",
        default=None,
    )

    args = parser.parse_args()

    # Load configuration from environment
    config = Config.from_env()

    display_name = (
        args.display_name
        if args.display_name
        else f"{args.first_name} {args.last_name}"
    )

    try:
        print(f"Creating user {args.username} ({args.email})...")

        # Get auth token
        token = get_lldap_token(config)

        # Create user
        user = create_user(
            config=config,
            token=token,
            username=args.username,
            email=args.email,
            first_name=args.first_name,
            last_name=args.last_name,
            display_name=display_name,
        )
        print(f"✓ User created: {user['email']}")

        # Send email
        send_onboarding_email(
            config=config,
            email=args.email,
            username=args.username,
            display_name=display_name,
        )
        print(f"✓ Onboarding email sent to {args.email}")

        print("\nDone! The user should receive an email with instructions.")

    except Exception as e:
        print(f"✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
