//! Short-lived, role-scoped Talos API credentials, minted on demand from the
//! admin talosconfig in 1Password.
//!
//! Talos authenticates with mTLS and has no notion of token revocation, so the
//! only real control over a leaked credential is that it expires quickly. This
//! tool keeps the one non-expiring credential — the `os:admin` talosconfig — in
//! 1Password, and derives a narrow, short-lived certificate from it whenever
//! one is actually needed.
//!
//! Every escalation necessarily goes back to 1Password, because Talos gates
//! `GenerateClientConfiguration` behind `os:admin`: a reader or operator
//! certificate cannot mint anything, not even another copy of itself. That is a
//! feature — it puts a biometric prompt and an audit-log entry in front of each
//! privilege escalation.

mod policy;
mod secrets;
mod store;
mod talos;

use std::ffi::OsString;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};

use policy::Role;

#[derive(Parser)]
#[command(
    name = "talos-session",
    version,
    about = "Mint short-lived, role-scoped Talos credentials from 1Password"
)]
struct Cli {
    /// 1Password secret reference for the os:admin talosconfig.
    #[arg(
        long,
        env = "TALOS_SESSION_OP_REF",
        default_value = "op://Homelab/talos/talosconfig",
        global = true
    )]
    op_ref: String,

    /// Talos endpoints to record in the minted credential. Defaults to whatever
    /// the admin talosconfig carries.
    #[arg(long, short = 'e', global = true, value_delimiter = ',')]
    endpoint: Vec<String>,

    /// Control-plane node that signs the certificate. Defaults to the first
    /// --endpoint, which is what you want when that endpoint is the VIP.
    #[arg(long, short = 'n', global = true)]
    node: Option<String>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Mint a credential into the well-known path $TALOSCONFIG points at.
    ///
    /// Always mints fresh: asking for a session is an explicit act, and the
    /// caller is entitled to a full-length one.
    Use {
        #[arg(value_enum)]
        role: Role,
    },

    /// Run a command with secrets exposed on file descriptors.
    ///
    /// A ROLE mints a short-lived Talos certificate and exposes it as
    /// TALOSCONFIG. Each --secret exposes a 1Password secret verbatim as
    /// $NAME. Both keep the secret in memory only, so nothing is left behind
    /// when the command exits.
    Exec {
        /// Mint a certificate for this role and expose it as TALOSCONFIG.
        #[arg(value_enum)]
        role: Option<Role>,

        /// Expose a 1Password secret as $NAME=/dev/fd/<n>, e.g.
        /// --secret TALOS_SECRETS=op://Homelab/talos/secrets.yaml
        #[arg(long = "secret", value_name = "NAME=REF", value_parser = parse_secret)]
        secrets: Vec<(String, String)>,

        /// Keep the minted certificate on the session tmpfs and reuse it while
        /// it is valid, so a batch of commands costs one 1Password unlock
        /// instead of one each. Raw --secret values are never cached: there is
        /// no derivation to amortise, only the fetch, and caching a root secret
        /// is the thing this tool exists to avoid.
        #[arg(long)]
        cache: bool,

        /// The command to run, after `--`.
        #[arg(last = true, required = true)]
        command: Vec<OsString>,
    },

    /// Show the credentials currently held.
    Status,

    /// Delete held credentials. Clears everything unless a role is named.
    Clear {
        #[arg(value_enum)]
        role: Option<Role>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let target = talos::Target {
        endpoints: cli.endpoint,
        node: cli.node,
    };

    match cli.command {
        Command::Use { role } => use_session(&cli.op_ref, role, &target),
        Command::Exec {
            role,
            secrets,
            cache,
            command,
        } => exec_session(&cli.op_ref, role, &secrets, cache, &target, &command),
        Command::Status => status(),
        Command::Clear { role } => clear(role),
    }
}

fn use_session(op_ref: &str, role: Role, target: &talos::Target) -> Result<()> {
    let dest = store::active_path()?;

    let admin = secrets::read(op_ref)?;
    talos::mint(&admin, role, &dest, target)?;

    eprintln!("Minted {} at {}", role.talos_role(), dest.display());
    eprint!("{}", talos::config_info(&dest)?);

    if std::env::var_os("TALOSCONFIG").is_none() {
        eprintln!(
            "\nNote: TALOSCONFIG is unset. Add this to your shell profile:\n  \
             export TALOSCONFIG=\"$XDG_RUNTIME_DIR/talos-session/config\""
        );
    }

    Ok(())
}

/// Parses a `--secret NAME=op://...` pair.
fn parse_secret(raw: &str) -> Result<(String, String), String> {
    let (name, reference) = raw
        .split_once('=')
        .ok_or_else(|| format!("expected NAME=REF, got {raw:?}"))?;

    if name.is_empty() || reference.is_empty() {
        return Err(format!("expected NAME=REF, got {raw:?}"));
    }

    Ok((name.to_string(), reference.to_string()))
}

fn exec_session(
    op_ref: &str,
    role: Option<Role>,
    refs: &[(String, String)],
    cache: bool,
    target: &talos::Target,
    command: &[OsString],
) -> Result<()> {
    if role.is_none() && refs.is_empty() {
        anyhow::bail!("nothing to expose: give a ROLE, one or more --secret, or both");
    }

    // The cached path deliberately keeps the certificate as a real file, so it
    // cannot also carry --secret values, which are never written down.
    if cache {
        let role = role.context("--cache applies to a minted certificate, so needs a ROLE")?;
        if !refs.is_empty() {
            anyhow::bail!("--cache cannot be combined with --secret: raw secrets are never cached");
        }

        let cached = store::cache_path(role)?;
        if !store::is_usable(&cached) {
            let admin = secrets::read(op_ref)?;
            talos::mint(&admin, role, &cached, target)?;
        }

        // Replaces this process, so nothing after this runs on success.
        return talos::exec_with(&cached, command);
    }

    let mut exposed = Vec::with_capacity(refs.len() + 1);

    if let Some(role) = role {
        let admin = secrets::read(op_ref)?;
        let config = talos::mint_ephemeral(&admin, role, &store::active_path()?, target)?;
        exposed.push(talos::Exposed {
            env: "TALOSCONFIG".to_string(),
            bytes: config,
        });
    }

    for (name, reference) in refs {
        exposed.push(talos::Exposed {
            env: name.clone(),
            bytes: secrets::read(reference)?,
        });
    }

    talos::exec_exposing(&exposed, command)
}

fn status() -> Result<()> {
    let held = store::existing()?;

    if held.is_empty() {
        println!("No credentials held. Mint one with `talos-session use <role>`.");
        return Ok(());
    }

    for (label, path) in held {
        let state = if store::is_usable(&path) {
            "valid"
        } else {
            "expired or unusable"
        };
        println!("{label} ({state}) — {}", path.display());

        match talos::config_info(&path) {
            Ok(info) => {
                for line in info.lines() {
                    println!("    {line}");
                }
            }
            Err(e) => println!("    could not read: {e}"),
        }
        println!();
    }

    Ok(())
}

fn clear(role: Option<Role>) -> Result<()> {
    match role {
        Some(role) => {
            let path = store::cache_path(role)?;
            store::remove(&path).with_context(|| format!("clearing {}", role.slug()))?;
            eprintln!("Cleared cached {}", role.talos_role());
        }
        None => {
            for (label, path) in store::existing()? {
                store::remove(&path)?;
                eprintln!("Cleared {label}");
            }
        }
    }

    Ok(())
}
