//! Role and lifetime policy.
//!
//! Talos enforces no server-side ceiling on `--crt-ttl` — the RPC handler
//! rejects only a non-positive duration — so these constants are the entirety
//! of the lifetime policy. They live here, hard-coded, rather than behind a
//! free-form `--ttl` flag, so that a slip of the keyboard cannot mint a
//! year-long admin certificate.

use std::time::Duration;

use clap::ValueEnum;

const HOUR: u64 = 60 * 60;

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
    fn slugs_are_distinct() {
        let slugs = [Role::Reader, Role::Operator, Role::Admin].map(Role::slug);
        assert_eq!(
            slugs.len(),
            slugs.iter().collect::<std::collections::HashSet<_>>().len()
        );
    }
}
