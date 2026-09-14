//! Where session credentials live, and how long they stay usable.
//!
//! Everything lands under `$XDG_RUNTIME_DIR`, which systemd already provides as
//! a per-user tmpfs mounted `mode=700,uid=<you>,nosuid,nodev` and clears at
//! logout. That is precisely the guarantee the old `sudo mount -t tmpfs` dance
//! was hand-rolling, so none of this needs root.

use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use base64::Engine;
use serde::Deserialize;

use crate::policy::Role;

/// Refuse to reuse a cached credential this close to its expiry, so a command
/// cannot die halfway through because the certificate lapsed mid-flight.
const EXPIRY_MARGIN: Duration = Duration::from_secs(5 * 60);

/// The subset of a talosconfig this tool needs to read back.
#[derive(Deserialize)]
struct TalosConfig {
    context: String,
    contexts: HashMap<String, TalosContext>,
}

#[derive(Deserialize)]
struct TalosContext {
    /// Base64-encoded PEM client certificate. Absent in a config that carries
    /// no client credential.
    crt: Option<String>,
}

/// The directory holding every credential this tool manages.
pub fn session_dir() -> Result<PathBuf> {
    let base = std::env::var_os("XDG_RUNTIME_DIR")
        .context("XDG_RUNTIME_DIR is not set — this tool relies on it for a user-private tmpfs")?;

    let dir = PathBuf::from(base).join("talos-session");
    ensure_private_dir(&dir)?;
    Ok(dir)
}

/// The well-known path that `$TALOSCONFIG` should point at permanently.
pub fn active_path() -> Result<PathBuf> {
    Ok(session_dir()?.join("config"))
}

/// Where a role's credential is cached for reuse by `exec`.
pub fn cache_path(role: Role) -> Result<PathBuf> {
    let dir = session_dir()?.join("cache");
    ensure_private_dir(&dir)?;
    Ok(dir.join(format!("{}.yaml", role.slug())))
}

/// Whether `path` holds a credential with enough life left to be worth reusing.
///
/// An unreadable or unparseable file is reported as unusable rather than as an
/// error: the caller's remedy in both cases is to mint a fresh one.
pub fn is_usable(path: &Path) -> bool {
    match not_after(path) {
        Ok(Some(expiry)) => expiry > SystemTime::now() + EXPIRY_MARGIN,
        _ => false,
    }
}

/// Reads the client certificate's expiry out of a talosconfig.
///
/// Returns `Ok(None)` for a well-formed config that carries no client
/// certificate.
pub fn not_after(path: &Path) -> Result<Option<SystemTime>> {
    let raw =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;

    let config: TalosConfig = serde_yaml_ng::from_str(&raw)
        .with_context(|| format!("parsing {} as a talosconfig", path.display()))?;

    let Some(context) = config.contexts.get(&config.context) else {
        anyhow::bail!(
            "{} names context {:?}, which it does not define",
            path.display(),
            config.context
        );
    };

    let Some(encoded) = context.crt.as_deref() else {
        return Ok(None);
    };

    let pem_bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .context("decoding the client certificate")?;

    let (_, pem) = x509_parser::pem::parse_x509_pem(&pem_bytes)
        .context("parsing the client certificate as PEM")?;
    let cert = pem
        .parse_x509()
        .context("parsing the client certificate as X.509")?;

    let seconds = cert.validity().not_after.timestamp();
    let seconds =
        u64::try_from(seconds).context("client certificate expires before the Unix epoch")?;

    Ok(Some(UNIX_EPOCH + Duration::from_secs(seconds)))
}

/// Every credential this tool currently holds, as (label, path) pairs.
pub fn existing() -> Result<Vec<(String, PathBuf)>> {
    let mut found = Vec::new();

    let active = active_path()?;
    if active.exists() {
        found.push(("active".to_string(), active));
    }

    for role in [Role::Reader, Role::Operator, Role::Admin] {
        let path = cache_path(role)?;
        if path.exists() {
            found.push((format!("cached:{}", role.slug()), path));
        }
    }

    Ok(found)
}

/// Removes a credential, if it is there. Missing files are not an error.
pub fn remove(path: &Path) -> Result<()> {
    match std::fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e).with_context(|| format!("removing {}", path.display())),
    }
}

/// Creates `dir` if needed and asserts it is owner-only.
///
/// `XDG_RUNTIME_DIR` is already 0700, so this guards against a stale directory
/// created by something else rather than against a hostile filesystem.
fn ensure_private_dir(dir: &Path) -> Result<()> {
    std::fs::create_dir_all(dir).with_context(|| format!("creating {}", dir.display()))?;
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))
        .with_context(|| format!("restricting {} to owner-only", dir.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_file_is_not_usable() {
        assert!(!is_usable(Path::new("/nonexistent/talos-session/config")));
    }

    #[test]
    fn config_without_a_client_certificate_has_no_expiry() {
        let dir = std::env::temp_dir().join(format!("talos-session-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("no-crt.yaml");
        std::fs::write(
            &path,
            "context: test\ncontexts:\n    test:\n        endpoints: []\n",
        )
        .unwrap();

        assert!(not_after(&path).unwrap().is_none());
        assert!(!is_usable(&path));

        std::fs::remove_dir_all(&dir).unwrap();
    }
}
