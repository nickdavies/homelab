//! Role and lifetime policy.
//!
//! Talos enforces no server-side ceiling on `--crt-ttl` — the RPC handler
//! rejects only a non-positive duration — so these constants are the entirety
//! of the Talos certificate lifetime policy. They live here, hard-coded, rather
//! than behind a free-form `--ttl` flag, so that a slip of the keyboard cannot
//! mint a year-long admin certificate.
//!
//! Kubeconfigs are the other way round: the `Kubeconfig` RPC takes no
//! arguments, and the control plane signs every one for its configured
//! `cluster.adminKubeconfig.certLifetime`. Here that is a ceiling checked
//! against what actually comes back, not a value sent.

use std::time::{Duration, SystemTime};

use anyhow::{bail, Result};
use clap::ValueEnum;

const HOUR: u64 = 60 * 60;

/// The longest-lived kubeconfig this tool will hand over. Matches
/// `patches-control-plane/admin-kubeconfig.yaml`; a control plane that missed
/// that patch signs for the Talos default of a year, and is refused.
pub const KUBECONFIG_TTL: Duration = Duration::from_secs(4 * HOUR);

/// Slack for clock skew between the signing node and this machine.
const CLOCK_SKEW: Duration = Duration::from_secs(5 * 60);

/// Refuses a kubeconfig whose certificate outlives [`KUBECONFIG_TTL`].
pub fn check_kubeconfig_expiry(not_after: SystemTime, now: SystemTime) -> Result<()> {
    let ceiling = now + KUBECONFIG_TTL + CLOCK_SKEW;
    if not_after > ceiling {
        let hours = not_after
            .duration_since(now)
            .map_or(0, |d| d.as_secs() / HOUR);
        bail!(
            "the control plane signed a kubeconfig valid for ~{hours}h, over the {}h ceiling; \
             is patches-control-plane/admin-kubeconfig.yaml applied to the node that signed it?",
            KUBECONFIG_TTL.as_secs() / HOUR
        );
    }
    Ok(())
}

/// A Talos API role this tool is willing to mint.
///
/// Deliberately narrower than the full Talos role set: `os:etcd:backup` exists
/// but is a service identity, not something a human drives interactively.
#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Role {
    /// Inspection only: `talosctl get`, `logs`, `dmesg`, `containers`,
    /// `services`, `version`, `disks`. Note that Talos puts `Read` and `Copy`
    /// (file *contents*) under admin, so this role can list files but not read
    /// them.
    Reader,
    /// Everything Reader can do, plus `reboot`, `shutdown`, `service restart`
    /// and `etcd snapshot`.
    Operator,
    /// Everything, including `apply-config`, `upgrade`, `reset`, `bootstrap`
    /// and reading file contents.
    Admin,
}

impl Role {
    /// The role string Talos expects in `--roles`.
    pub fn talos_role(self) -> &'static str {
        match self {
            Role::Reader => "os:reader",
            Role::Operator => "os:operator",
            Role::Admin => "os:admin",
        }
    }

    /// How long a certificate for this role may live.
    ///
    /// Reader is generous because the credential is near-harmless and lives on
    /// a tmpfs that is cleared at logout anyway; admin is short because it is
    /// the tier that can rewrite machine config.
    pub fn ttl(self) -> Duration {
        match self {
            Role::Reader => Duration::from_secs(30 * 24 * HOUR),
            Role::Operator => Duration::from_secs(48 * HOUR),
            Role::Admin => Duration::from_secs(2 * HOUR),
        }
    }

    /// Stable, filesystem-safe name used for cache files.
    pub fn slug(self) -> &'static str {
        match self {
            Role::Reader => "reader",
            Role::Operator => "operator",
            Role::Admin => "admin",
        }
    }
}

/// Formats a duration the way `talosctl --crt-ttl` wants it.
///
/// Go's duration parser has no day unit, so everything is rendered as hours.
pub fn ttl_arg(ttl: Duration) -> String {
    format!("{}h", ttl.as_secs() / HOUR)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ttls_are_ordered_by_privilege() {
        assert!(Role::Admin.ttl() < Role::Operator.ttl());
        assert!(Role::Operator.ttl() < Role::Reader.ttl());
    }

    #[test]
    fn ttl_arg_renders_whole_hours() {
        assert_eq!(ttl_arg(Role::Admin.ttl()), "2h");
        assert_eq!(ttl_arg(Role::Operator.ttl()), "48h");
        assert_eq!(ttl_arg(Role::Reader.ttl()), "720h");
    }

    #[test]
    fn kubeconfig_within_the_ceiling_is_accepted() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        assert!(check_kubeconfig_expiry(now + KUBECONFIG_TTL, now).is_ok());
        assert!(check_kubeconfig_expiry(now + KUBECONFIG_TTL + CLOCK_SKEW, now).is_ok());
    }

    #[test]
    fn year_long_kubeconfig_is_refused() {
        let now = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000);
        let year = Duration::from_secs(365 * 24 * HOUR);
        let err = check_kubeconfig_expiry(now + year, now).unwrap_err();
        assert!(err.to_string().contains("~8760h"), "{err}");
    }

    #[test]
    fn slugs_are_distinct() {
        let slugs = [Role::Reader, Role::Operator, Role::Admin].map(Role::slug);
        assert_eq!(
            slugs.len(),
            slugs.iter().collect::<std::collections::HashSet<_>>().len()
        );
    }
}
