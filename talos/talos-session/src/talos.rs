//! Handing secrets to a child process without letting them touch a filesystem.
//!
//! Every consumer here — `talosctl`, `kubectl --from-file`, `flux
//! --private-key-file` — takes its secret as a *path*, so each one has to be
//! addressable as a file. Rather than write them out, even briefly, even on a
//! tmpfs, each secret is held in an anonymous `memfd` and the child is pointed
//! at `/dev/fd/<n>`. They exist only in that process tree's memory and are
//! reaped when it exits.
//!
//! Minting a short-lived Talos certificate is the same pipeline with one extra
//! step in the middle: the root credential goes in on a descriptor, and what
//! comes back out is what the child gets.

use std::ffi::{CString, OsString};
use std::fs::File;
use std::io::{self, Seek, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Context, Result};

use crate::policy::{ttl_arg, Role};

/// A secret to expose to a child process, and the environment variable that
/// will carry its path.
pub struct Exposed {
    pub env: String,
    pub bytes: Vec<u8>,
}

/// Which Talos node signs a credential, and what endpoints it records.
#[derive(Debug, Default)]
pub struct Target {
    /// Endpoints written into the minted credential. Empty inherits whatever
    /// the admin talosconfig carries.
    pub endpoints: Vec<String>,
    /// The node whose CA signs the certificate. `talosctl config new` insists on
    /// exactly one, so this falls back to the first endpoint — for a
    /// control-plane VIP the two are the same address.
    pub node: Option<String>,
}

impl Target {
    fn node(&self) -> Option<&str> {
        self.node
            .as_deref()
            .or_else(|| self.endpoints.first().map(String::as_str))
    }
}

/// Mints a role-scoped talosconfig at `dest`, authenticating as `admin_config`.
///
/// `talosctl config new` refuses to overwrite an existing file, so the
/// certificate is minted to a sibling temporary path and renamed into place.
/// The rename is atomic, so a concurrent reader sees either the old credential
/// or the new one, never a half-written file.
pub fn mint(admin_config: &[u8], role: Role, dest: &Path, target: &Target) -> Result<()> {
    let staging = staging_path(dest);
    mint_to(admin_config, role, &staging, target)?;

    std::fs::rename(&staging, dest).with_context(|| {
        format!(
            "installing credential at {} (from {})",
            dest.display(),
            staging.display()
        )
    })?;

    Ok(())
}

/// Mints a role-scoped talosconfig and returns it in memory, leaving nothing
/// behind on disk.
///
/// `talosctl config new` can only write to a path, so the credential does touch
/// the (0700, user-private, tmpfs) session directory for the moment between
/// talosctl writing it and this function reading it back. `near` fixes which
/// directory that is.
pub fn mint_ephemeral(
    admin_config: &[u8],
    role: Role,
    near: &Path,
    target: &Target,
) -> Result<Vec<u8>> {
    let staging = staging_path(near);
    mint_to(admin_config, role, &staging, target)?;

    let bytes = std::fs::read(&staging)
        .with_context(|| format!("reading the minted credential at {}", staging.display()));
    let _ = std::fs::remove_file(&staging);

    bytes
}

/// Runs `talosctl config new`, leaving the result at `staging`.
///
/// `config new` treats any existing file as fatal, so a leftover from an earlier
/// crash is cleared first, and a failed run does not leave one behind.
fn mint_to(admin_config: &[u8], role: Role, staging: &Path, target: &Target) -> Result<()> {
    let parent = staging
        .parent()
        .context("destination path has no parent directory")?;
    std::fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;

    let _ = std::fs::remove_file(staging);

    let mut args = vec![
        "config".to_string(),
        "new".to_string(),
        format!("--roles={}", role.talos_role()),
        format!("--crt-ttl={}", ttl_arg(role.ttl())),
    ];
    if !target.endpoints.is_empty() {
        args.push("--endpoints".to_string());
        args.push(target.endpoints.join(","));
    }
    if let Some(node) = target.node() {
        args.push("--nodes".to_string());
        args.push(node.to_string());
    }
    args.push(staging.to_string_lossy().into_owned());

    let result = run_as_admin(admin_config, &args);
    if result.is_err() {
        let _ = std::fs::remove_file(staging);
    }

    result
}

/// Renders `talosctl config info` for an existing credential.
///
/// Shelling out keeps role and expiry reporting authoritative rather than
/// re-deriving it from the certificate here.
pub fn config_info(config: &Path) -> Result<String> {
    let output = Command::new("talosctl")
        .arg("config")
        .arg("info")
        .arg("--talosconfig")
        .arg(config)
        .output()
        .context("running `talosctl`")?;

    if !output.status.success() {
        bail!(
            "talosctl config info failed: {}",
            diagnostic(&output.stderr)
        );
    }

    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Replaces this process with `argv`, with `TALOSCONFIG` pointing at `config`.
///
/// Used for the cached path, where the credential is deliberately a real file.
/// Replacing rather than spawning keeps signal handling and the exit status
/// exactly as if the user had run the command directly.
pub fn exec_with(config: &Path, argv: &[OsString]) -> Result<()> {
    let (program, args) = argv.split_first().context("no command given")?;

    // exec() only returns if it failed.
    let err = Command::new(program)
        .args(args)
        .env("TALOSCONFIG", config)
        .exec();

    Err(err).with_context(|| format!("running {}", program.to_string_lossy()))
}

/// Replaces this process with `argv`, handing it each secret on its own
/// descriptor rather than through a file.
///
/// The descriptors are whichever numbers `memfd_create` returned; there is no
/// need to move them onto fixed slots, so nothing can collide. Each is exported
/// as `<env>=/dev/fd/<n>`.
pub fn exec_exposing(exposed: &[Exposed], argv: &[OsString]) -> Result<()> {
    let (program, args) = argv.split_first().context("no command given")?;

    let mut cmd = Command::new(program);
    cmd.args(args);

    // Held until exec replaces us; dropping them would close the descriptors.
    let mut fds = Vec::with_capacity(exposed.len());
    for secret in exposed {
        let fd = memfd_with(&secret.bytes)?;
        let raw = fd.as_raw_fd();

        // exec replaces the current process rather than forking, so the
        // descriptor is unsealed here and inherited directly.
        clear_cloexec(raw)
            .with_context(|| format!("exposing {} to the child process", secret.env))?;

        cmd.env(&secret.env, format!("/dev/fd/{raw}"));
        fds.push(fd);
    }

    let err = cmd.exec();

    drop(fds);
    Err(err).with_context(|| format!("running {}", program.to_string_lossy()))
}

/// Runs `talosctl <args>` authenticated by `admin_config`.
fn run_as_admin(admin_config: &[u8], args: &[String]) -> Result<()> {
    let fd = memfd_with(admin_config)?;
    let raw = fd.as_raw_fd();

    let mut cmd = Command::new("talosctl");
    cmd.args(args).env("TALOSCONFIG", format!("/dev/fd/{raw}"));

    // SAFETY: the closure calls only fcntl, which is async-signal-safe — the
    // contract pre_exec imposes on code running between fork and exec. The
    // descriptor is unsealed in the child so it does not leak to other children
    // of this process.
    unsafe {
        cmd.pre_exec(move || clear_cloexec(raw));
    }

    let output = cmd.output().context("running `talosctl`")?;
    if !output.status.success() {
        bail!("talosctl config new failed: {}", diagnostic(&output.stderr));
    }

    Ok(())
}

/// Trims a talosctl failure down to the part worth reading.
///
/// Cobra appends its full usage text to any flag or argument error, which
/// buries the one line that actually says what went wrong.
fn diagnostic(stderr: &[u8]) -> String {
    String::from_utf8_lossy(stderr)
        .lines()
        .take_while(|line| !line.starts_with("Usage:"))
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// Copies `bytes` into an anonymous in-memory file, rewound ready for reading.
fn memfd_with(bytes: &[u8]) -> Result<OwnedFd> {
    let name = CString::new("secret").expect("literal contains no NUL");

    // SAFETY: `name` is a valid NUL-terminated string that outlives the call.
    let raw = unsafe { libc::memfd_create(name.as_ptr(), libc::MFD_CLOEXEC) };
    if raw < 0 {
        return Err(io::Error::last_os_error()).context("creating an in-memory file for a secret");
    }

    // SAFETY: memfd_create just handed us an owned descriptor we have not
    // shared with anything else.
    let mut file = unsafe { File::from_raw_fd(raw) };
    file.write_all(bytes)
        .context("writing a secret to memory")?;
    file.rewind().context("rewinding a secret")?;

    Ok(OwnedFd::from(file))
}

/// Clears `FD_CLOEXEC` so the descriptor survives an exec.
///
/// Called from `pre_exec` on the spawn path, so it must stick to
/// async-signal-safe calls — `fcntl` qualifies.
fn clear_cloexec(raw: RawFd) -> io::Result<()> {
    // SAFETY: `raw` is open in this process.
    let flags = unsafe { libc::fcntl(raw, libc::F_GETFD) };
    if flags < 0 {
        return Err(io::Error::last_os_error());
    }

    // SAFETY: as above.
    if unsafe { libc::fcntl(raw, libc::F_SETFD, flags & !libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }

    Ok(())
}

/// Sibling path used to stage a freshly minted credential before renaming it
/// over `dest`. Includes the pid so two concurrent runs cannot collide.
fn staging_path(dest: &Path) -> PathBuf {
    let mut name = dest.file_name().unwrap_or_default().to_os_string();
    name.push(format!(".new.{}", std::process::id()));
    dest.with_file_name(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn staging_path_is_a_sibling_of_the_destination() {
        let dest = Path::new("/run/user/1000/talos-session/config");
        let staging = staging_path(dest);

        assert_eq!(staging.parent(), dest.parent());
        assert_ne!(staging, dest);
        assert!(staging
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with("config.new."));
    }

    #[test]
    fn node_falls_back_to_the_first_endpoint() {
        let inherited = Target::default();
        assert_eq!(inherited.node(), None);

        let from_endpoint = Target {
            endpoints: vec!["192.168.254.10".into(), "192.168.254.11".into()],
            node: None,
        };
        assert_eq!(from_endpoint.node(), Some("192.168.254.10"));

        let explicit = Target {
            endpoints: vec!["192.168.254.10".into()],
            node: Some("192.168.254.11".into()),
        };
        assert_eq!(explicit.node(), Some("192.168.254.11"));
    }

    #[test]
    fn diagnostic_drops_the_cobra_usage_block() {
        let stderr = b"nodes are not set for the command\n\nUsage:\n  talosctl config new\n\nFlags:\n  -h, --help\n";
        assert_eq!(diagnostic(stderr), "nodes are not set for the command");
    }

    #[test]
    fn memfd_round_trips_the_credential() {
        use std::io::Read;

        let fd = memfd_with(b"context: test\n").expect("memfd");
        let mut file = File::from(fd);
        let mut got = String::new();
        file.read_to_string(&mut got).expect("read back");

        assert_eq!(got, "context: test\n");
    }

    #[test]
    fn clear_cloexec_unsets_the_flag() {
        let fd = memfd_with(b"x").expect("memfd");
        let raw = fd.as_raw_fd();

        // SAFETY: raw is open and owned by `fd`.
        let before = unsafe { libc::fcntl(raw, libc::F_GETFD) };
        assert_eq!(before & libc::FD_CLOEXEC, libc::FD_CLOEXEC);

        clear_cloexec(raw).expect("clear");

        // SAFETY: as above.
        let after = unsafe { libc::fcntl(raw, libc::F_GETFD) };
        assert_eq!(after & libc::FD_CLOEXEC, 0);
    }
}
