//! Reading the root admin talosconfig out of 1Password.
//!
//! Shelling out to `op` rather than using the 1Password SDK is deliberate: the
//! SDK authenticates with a service-account token, which would be a long-lived
//! credential sitting on disk — exactly what this tool exists to avoid. The CLI
//! reuses the desktop app's biometric-backed session instead, so every
//! escalation is a deliberate, human-approved act that lands in 1Password's own
//! audit log.

use std::process::Command;

use anyhow::{bail, Context, Result};

/// Fetches the secret at `op_ref` and returns it verbatim.
///
/// The bytes are the admin talosconfig; they are returned rather than written
/// anywhere so the caller can hand them to `talosctl` over an anonymous
/// in-memory file. See [`crate::talos`].
pub fn read(op_ref: &str) -> Result<Vec<u8>> {
    let output = Command::new("op")
        .args(["read", "--no-newline", op_ref])
        .output()
        .context("running `op` — is the 1Password CLI installed and on PATH?")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("not signed in") {
            bail!("1Password is locked. Unlock it (`op signin`) and try again.");
        }
        bail!("`op read {op_ref}` failed: {}", stderr.trim());
    }

    if output.stdout.is_empty() {
        bail!("`op read {op_ref}` returned nothing — check the secret reference");
    }

    Ok(output.stdout)
}
