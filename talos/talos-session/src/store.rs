//! Where session credentials live, and how long they stay usable.
//!
//! Everything lands under `$XDG_RUNTIME_DIR`, which systemd already provides as
//! a per-user tmpfs mounted `mode=700,uid=<you>,nosuid,nodev` and clears at
//! logout. That is precisely the guarantee the old `sudo mount -t tmpfs` dance
//! was hand-rolling, so none of this needs root.

use std::collections::HashMap;
use std::io::Write;
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
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

/// The well-known path that `$KUBECONFIG` should point at.
pub fn kubeconfig_path() -> Result<PathBuf> {
    Ok(session_dir()?.join("kubeconfig"))
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

    context.crt.as_deref().map(cert_not_after).transpose()
}

/// The subset of a kubeconfig this tool needs to read back.
#[derive(Deserialize)]
struct KubeConfig {
    #[serde(rename = "current-context")]
    current_context: String,
    contexts: Vec<NamedKubeContext>,
    users: Vec<NamedKubeUser>,
}

#[derive(Deserialize)]
struct NamedKubeContext {
    name: String,
    context: KubeContext,
}

#[derive(Deserialize)]
struct KubeContext {
    user: String,
}

#[derive(Deserialize)]
struct NamedKubeUser {
    name: String,
    user: KubeUser,
}

#[derive(Deserialize)]
struct KubeUser {
    #[serde(rename = "client-certificate-data")]
    client_certificate_data: Option<String>,
}

/// Reads the client certificate's expiry out of a kubeconfig.
///
/// A kubeconfig without a client certificate is an error, not `None`: every
/// one Talos issues carries one, so its absence means this is not what was
/// asked for.
pub fn kubeconfig_not_after(raw: &[u8]) -> Result<SystemTime> {
    let config: KubeConfig = serde_yaml_ng::from_slice(raw).context("parsing the kubeconfig")?;

    let context = config
        .contexts
        .iter()
        .find(|c| c.name == config.current_context)
        .with_context(|| {
            format!(
                "kubeconfig names context {:?}, which it does not define",
                config.current_context
            )
        })?;

    let user = config
        .users
        .iter()
        .find(|u| u.name == context.context.user)
        .with_context(|| {
            format!(
                "kubeconfig context names user {:?}, which it does not define",
                context.context.user
            )
        })?;

    let encoded = user
        .user
        .client_certificate_data
        .as_deref()
        .context("kubeconfig carries no client certificate")?;

    cert_not_after(encoded)
}

/// Expiry of a base64-encoded PEM certificate, the form both talosconfig and
/// kubeconfig embed it in.
fn cert_not_after(encoded: &str) -> Result<SystemTime> {
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

    Ok(UNIX_EPOCH + Duration::from_secs(seconds))
}

/// Writes `bytes` to `dest`, owner-only, via a sibling file renamed into place
/// so a concurrent reader never sees a half-written credential.
pub fn install_private(dest: &Path, bytes: &[u8]) -> Result<()> {
    let mut name = dest.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".new.{}", std::process::id()));
    let staging = dest.with_file_name(name);

    let _ = std::fs::remove_file(&staging);
    let written = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&staging)
        .and_then(|mut f| f.write_all(bytes))
        .and_then(|()| std::fs::rename(&staging, dest));

    if written.is_err() {
        let _ = std::fs::remove_file(&staging);
    }
    written.with_context(|| format!("installing credential at {}", dest.display()))
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

    /// Throwaway self-signed `O=system:masters` certificate, valid until
    /// 2026-11-21T04:00:00Z. Its key was discarded at generation.
    const FIXTURE_CRT: &str = "LS0tLS1CRUdJTiBDRVJUSUZJQ0FURS0tLS0tCk1JSUJwekNDQVUyZ0F3SUJBZ0lVUk1GQlVOOG9reUI5a3dBTHRuZjJpYVFFUXNzd0NnWUlLb1pJemowRUF3SXcKS1RFWE1CVUdBMVVFQ2d3T2MzbHpkR1Z0T20xaGMzUmxjbk14RGpBTUJnTlZCQU1NQldGa2JXbHVNQjRYRFRJMgpNVEV5TVRBd01EQXdNRm9YRFRJMk1URXlNVEEwTURBd01Gb3dLVEVYTUJVR0ExVUVDZ3dPYzNsemRHVnRPbTFoCmMzUmxjbk14RGpBTUJnTlZCQU1NQldGa2JXbHVNRmt3RXdZSEtvWkl6ajBDQVFZSUtvWkl6ajBEQVFjRFFnQUUKb3NUbjFrWHBobzlJVEpPTkRpbEd2OGVtdDZMbXB6WWtMRlRHQlFMNFJqeU0zK3hHNk5hZWdOTTdyVURFR0lqNQpTQXAwb2FJYmVGRTU5bkxuYXEzeG5hTlRNRkV3SFFZRFZSME9CQllFRkgvMS9oZmJIZkZvdDdpMlZ6QlNvSC9UCmdHWFNNQjhHQTFVZEl3UVlNQmFBRkgvMS9oZmJIZkZvdDdpMlZ6QlNvSC9UZ0dYU01BOEdBMVVkRXdFQi93UUYKTUFNQkFmOHdDZ1lJS29aSXpqMEVBd0lEU0FBd1JRSWdFR05nMVdVNmZpTE1vWDFUQ3A1SkxzUE94cUN6WWlhOQpwUjhra3U3S3JFd0NJUUM4clBVMUN1dkNhaGNMZzJudlJWSFcraGtVNTIyU0ZqS0dyWUhkQ1l4SDFBPT0KLS0tLS1FTkQgQ0VSVElGSUNBVEUtLS0tLQo=";
    const FIXTURE_NOT_AFTER: u64 = 1_795_233_600;

    /// The shape `talosctl kubeconfig` emits.
    fn kubeconfig(current: &str, user_crt: Option<&str>) -> String {
        let crt = user_crt
            .map(|c| format!("\n        client-certificate-data: {c}"))
            .unwrap_or_default();
        format!(
            "apiVersion: v1\nkind: Config\nclusters:\n- name: homelab\n  cluster:\n        server: https://192.168.254.10:6443\ncontexts:\n- name: admin@homelab\n  context:\n        cluster: homelab\n        namespace: default\n        user: admin@homelab\ncurrent-context: {current}\nusers:\n- name: admin@homelab\n  user:\n        client-key-data: unused{crt}\n"
        )
    }

    #[test]
    fn kubeconfig_expiry_comes_from_the_current_users_certificate() {
        let raw = kubeconfig("admin@homelab", Some(FIXTURE_CRT));
        assert_eq!(
            kubeconfig_not_after(raw.as_bytes()).unwrap(),
            UNIX_EPOCH + Duration::from_secs(FIXTURE_NOT_AFTER)
        );
    }

    #[test]
    fn kubeconfig_without_a_certificate_is_an_error() {
        let raw = kubeconfig("admin@homelab", None);
        assert!(kubeconfig_not_after(raw.as_bytes()).is_err());
    }

    #[test]
    fn kubeconfig_with_a_dangling_context_is_an_error() {
        let raw = kubeconfig("other@homelab", Some(FIXTURE_CRT));
        let err = kubeconfig_not_after(raw.as_bytes()).unwrap_err();
        assert!(err.to_string().contains("other@homelab"), "{err}");
    }

    #[test]
    fn install_private_writes_owner_only() {
        let dir =
            std::env::temp_dir().join(format!("talos-session-install-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let dest = dir.join("kubeconfig");

        install_private(&dest, b"first").unwrap();
        install_private(&dest, b"second").unwrap();

        assert_eq!(std::fs::read(&dest).unwrap(), b"second");
        let mode = std::fs::metadata(&dest).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
        assert_eq!(
            std::fs::read_dir(&dir).unwrap().count(),
            1,
            "staging file left behind"
        );

        std::fs::remove_dir_all(&dir).unwrap();
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
