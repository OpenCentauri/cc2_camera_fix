mod protocol;
mod usb;
use std::{io::Read, time::{Duration, Instant}};
use protocol::{send, upload};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const ACCEPT: &str = "--accept-no-backup-space-risk";
const HELP: &str = "cc2camera-hid: experimental prevention for a working stock CC2 camera

Run as root on an idle printer. ARMv7 Linux; no Python, ADB, hidraw or shared
libraries required by the static build. Only known camera firmware is supported.

  cc2camera-hid inspect
      Read USB descriptors; query the version only for a supported HID camera.
      Recognized 30D signatures need no patch and receive no camera commands.
  cc2camera-hid install --accept-no-backup-space-risk
      Install the persistent erase-fix hook through HID. NO BACKUP is exported,
      and safe clean space is NOT checked. A failed write or power loss can leave
      the camera unbootable and require an external SPI programmer to recover.
      Firmware checks and installed-file comparisons run on the camera.
  cc2camera-hid verify
      After a successful installation and printer restart, upload a temporary
      probe and verify the installed hooks and live RAM correction. No persistent
      file or RAM correction writes; temporary files/status are written in /tmp.

Install optionally accepts --overwrite-managed-scripts to replace differing
contents of system.sh and enabled/10-erase-fix.sh. Custom behavior may be lost.
Originals are retained in camera RAM for this boot, not exported or durable.
Other enabled scripts remain untouched. Default: refuse differing contents.

Add --verbose to any command to log every full HID report in hex to stderr,
including upload payloads, malformed replies and transport errors. No retries.

Install/verify launch a camera shell script, temporarily replace its version
response for two minutes, and leave its HID uploader unavailable until restart.
Never retry after an error. A timeout does not cancel a camera-side installer.
This route requires physical validation; a USB acknowledgement is not success.
";

#[derive(Debug, PartialEq)]
enum Action { Help, Inspect, Install, Verify }
fn arguments(args: &[String]) -> Result<Action> {
    match args.iter().map(String::as_str).collect::<Vec<_>>().as_slice() {
        [] | ["--help"] | ["-h"] => Ok(Action::Help),
        ["inspect"] => Ok(Action::Inspect),
        ["verify"] => Ok(Action::Verify),
        ["install", flag] if *flag==ACCEPT => Ok(Action::Install),
        ["install"] => Err(format!("Installation risks an unbootable camera with no exported backup or clean-space check. Read --help; explicit {ACCEPT} is required before USB access.").into()),
        _ => Err("unknown arguments; run --help".into()),
    }
}
fn options(args: &[String]) -> Result<(Action, bool, bool)> {
    let count = args.iter().filter(|a| a.as_str() == "--verbose").count();
    if count > 1 { return Err("--verbose may only be specified once".into()); }
    let overwrite = args.iter().filter(|a| a.as_str() == "--overwrite-managed-scripts").count();
    if overwrite > 1 { return Err("--overwrite-managed-scripts may only be specified once".into()); }
    let filtered: Vec<String> = args.iter().filter(|a| !matches!(a.as_str(), "--verbose" | "--overwrite-managed-scripts")).cloned().collect();
    let action = arguments(&filtered)?;
    if overwrite != 0 && action != Action::Install { return Err("--overwrite-managed-scripts requires install".into()); }
    Ok((action, count == 1, overwrite == 1))
}
fn script(token: &str, action: &Action, overwrite: bool) -> Result<(String, String, Vec<u8>)> {
    if token.len()!=16 || !token.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()) {
        return Err("invalid local session token".into());
    }
    if overwrite && *action != Action::Install { return Err("overwrite requires install".into()); }
    let mode=match action { Action::Install=>"install", Action::Verify=>"verify", _=>return Err("invalid script action".into()) };
    let path=format!("/tmp/.cc2-hid-{token}.sh");
    // Literal fopen must fail (the embedded /bin/sh makes a nonexistent directory
    // component). Only the preceding shell's rm command launches our worker.
    let launch=format!("/tmp/.cc2-launch-{token};/bin/sh${{IFS}}{path}&");
    let data=include_str!("../payload/installer.sh").replace("@TOKEN@",token).replace("@MODE@",mode).replace("@OVERWRITE@", if overwrite {"1"} else {"0"}).into_bytes();
    if launch.len()>127 || data.len()>32768 { return Err("embedded installer exceeds protocol limits".into()); }
    Ok((path,launch,data))
}
fn terminal(payload: &[u8], token: &str, action: &Action) -> Result<bool> {
    let prefix=format!("{token}:");
    if !payload.starts_with(prefix.as_bytes()) { return Ok(false); }
    let state = &payload[prefix.len()..];
    if state.len() == 3 && matches!(state[0], b'F' | b'P') && state[1..].iter().all(u8::is_ascii_digit) {
        let code = std::str::from_utf8(&state[1..])?;
        let reason = include_str!("../failure-reasons.txt").lines()
            .filter_map(|line| line.split_once(' '))
            .find(|(id, _)| *id == code).map(|(_, name)| name).unwrap_or("unknown-check");
        let detail = if state[0] == b'F' {
            "no persistent installation writes were started. Do not retry this boot."
        } else {
            "persistent writes began; outcome may be partial. Preserve power; do not retry or restart blindly."
        };
        return Err(format!("camera-side check failed: {reason} ({}); {detail}", String::from_utf8_lossy(state)).into());
    }
    match state {
        b"BUSY" => Ok(false),
        b"DONE" | b"SAME" if *action==Action::Install => Ok(true),
        b"LIVE" if *action==Action::Verify => Ok(true),
        b"FAIL" => Err("camera-side checks refused the operation; no persistent installation writes were started. Do not retry this boot.".into()),
        b"PART" => Err("installation failed after persistent writes began; camera may be unbootable. Do not retry or restart blindly; preserve power and seek recovery using the documented backup/programmer route.".into()),
        _ => Err("unexpected camera operation status".into()),
    }
}
const NEWER_MESSAGE: &str = "USB signature matches the observed EF-S7-V1.0.30D camera. This patch does not apply to that revision. No camera commands sent.";
fn with_hid_camera(
    found: usb::Discovery,
    action: &Action,
    use_camera: impl FnOnce(usb::Camera) -> Result<()>,
) -> Result<()> {
    match found {
        usb::Discovery::Newer30d if *action == Action::Inspect => {
            println!("{NEWER_MESSAGE}");
            Ok(())
        }
        usb::Discovery::Newer30d => Err(NEWER_MESSAGE.into()),
        usb::Discovery::Hid(camera) => use_camera(camera),
    }
}
/// Send only the read-only version query; reject before any upload on failure.
fn query_version(transport: &mut impl protocol::Transport) -> Result<Option<Vec<u8>>> {
    let (status, version) = send(transport, 1, &[], 1, 0)?;
    if version_unavailable(status, &version) { return Ok(None); }
    let reason = if status != 0 {
        Some("camera returned a nonzero status")
    } else if version.is_empty() {
        Some("empty version payload")
    } else if version.len() > 23 {
        Some("version payload exceeds 23 bytes")
    } else if !version.iter().all(|b| (0x20..=0x7e).contains(b)) {
        Some("version payload contains non-printable bytes")
    } else {
        None
    };
    if let Some(reason) = reason {
        let preview = &version[..version.len().min(64)];
        return Err(format!(
            "invalid camera version response: {reason}; command=0x0001; status={status} (0x{status:08x}); payload_len={}; escaped=\"{}\"; hex={:02x?}{}. No upload was started.",
            version.len(), preview.escape_ascii(), preview,
            if preview.len() < version.len() { " (preview truncated to 64 bytes)" } else { "" }
        ).into());
    }
    Ok(Some(version))
}
// The stock handler returns this exact reply when opening/reading its RAM file fails.
fn version_unavailable(status: u32, payload: &[u8]) -> bool {
    status == 1 && payload.is_empty()
}
fn run() -> Result<()> {
    // Parse consent and prepare every outgoing target before touching USB.
    let (action, verbose, overwrite)=options(&std::env::args().skip(1).collect::<Vec<_>>())?;
    if action==Action::Help { print!("{HELP}"); return Ok(()); }
    let mut random=[0u8;8];
    let prepared=if matches!(action,Action::Install|Action::Verify) {
        std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
        let token=format!("{:016x}", u64::from_be_bytes(random));
        let payload=script(&token,&action,overwrite)?;
        Some((token,payload))
    } else { None };
    with_hid_camera(usb::discover()?, &action, |camera| {
    println!("Camera {}: HID interface {}, input 0x{:02x}, output {:?}",camera.path,camera.interface.number,camera.interface.input,camera.interface.output);
    let mut transport=protocol::Trace { inner: usb::Usb::open(camera)?, writer: std::io::stderr(), enabled: verbose };
    let version=query_version(&mut transport)?;
    match version {
        Some(version) => println!("Camera version: {}", String::from_utf8_lossy(&version)),
        None => println!("Camera version unavailable (status 1, empty reply). HID communication works; firmware compatibility is not yet verified."),
    }
    if let Some((token,(path,launch,data)))=prepared {
        if action==Action::Install { println!("Accepted: no exported backup or clean-space check; programmer recovery may be required."); }
        if overwrite { println!("Accepted: replace differing system.sh and enabled/10-erase-fix.sh. Previous custom behavior may be lost. Existing originals are retained in camera RAM at /tmp/.cc2-old-scripts-{token}, lost on reboot."); }
        println!("Uploading temporary camera worker; session {token}. Do not interrupt power.");
        upload(&mut transport,path.as_bytes(),&data,false)?;
        upload(&mut transport,launch.as_bytes(),b"\n",true)?;
        let deadline=Instant::now()+Duration::from_secs(90);
        while Instant::now()<deadline {
            let (status,payload)=send(&mut transport,1,&[],1,0)?;
            // The background worker may not have created the status file yet.
            // Waiting is bounded by the same deadline; no upload is retried.
            if version_unavailable(status, &payload) {
                std::thread::sleep(Duration::from_millis(500));
                continue;
            }
            if status!=0 { return Err("camera status query failed".into()); }
            if terminal(&payload,&token,&action)? {
                if action==Action::Install {
                    println!("Camera reports exact installed hook contents and permissions verified. Activation is not yet verified. Wait at least two minutes, restart the idle printer, then run cc2camera-hid verify.");
                } else { println!("Camera reports canonical hooks and the live erase correction verified for this boot."); }
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        return Err("timed out: outcome unknown; camera worker may still be running".into());
    }
    Ok(())
    })
}
fn main() {
    if let Err(e)=run() {
        eprintln!("Error: {e}\nNo automatic retry was performed. If install/verify upload began, preserve power and diagnostics; do not infer success or restart solely from this error.");
        std::process::exit(1);
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn args(a:&[&str])->Vec<String> {a.iter().map(|s|s.to_string()).collect()}
    #[test]
    fn newer_camera_never_reaches_usb_open_for_any_command() {
        for action in [Action::Inspect, Action::Install, Action::Verify] {
            let result = with_hid_camera(usb::Discovery::Newer30d, &action, |_| {
                panic!("30D must never reach USB open, interface claim or command sending")
            });
            assert_eq!(result.is_ok(), action == Action::Inspect);
        }
    }
    #[test]
    fn failure_codes_preserve_stage_and_require_current_session() {
        for line in include_str!("../failure-reasons.txt").lines() {
            let (code, reason) = line.split_once(' ').unwrap();
            for (stage, message) in [("F", "no persistent installation writes"), ("P", "persistent writes began")] {
                let payload = format!("0123456789abcdef:{stage}{code}");
                assert!(payload.len() <= 23);
                let error = terminal(payload.as_bytes(), "0123456789abcdef", &Action::Install).unwrap_err().to_string();
                assert!(error.contains(reason) && error.contains(message), "{error}");
                assert!(!terminal(payload.as_bytes(), "fedcba9876543210", &Action::Install).unwrap());
            }
        }
        for state in ["F00", "P99", "F1", "F010", "Fxx"] {
            assert!(terminal(format!("0123456789abcdef:{state}").as_bytes(), "0123456789abcdef", &Action::Install).is_err());
        }
    }
    #[test]
    fn verbose_preserves_consent_and_argument_refusals() {
        assert_eq!(options(&args(&["--verbose", "inspect"])).unwrap(), (Action::Inspect, true, false));
        assert_eq!(options(&args(&["inspect", "--verbose"])).unwrap(), (Action::Inspect, true, false));
        assert_eq!(options(&args(&["install", ACCEPT, "--verbose"])).unwrap(), (Action::Install, true, false));
        assert!(options(&args(&["install", "--verbose"])).is_err());
        assert!(options(&args(&["inspect", "--verbose", "--verbose"])).is_err());
        assert!(options(&args(&["inspect", "--unknown", "--verbose"])).is_err());
    }
    #[test]
    fn overwrite_is_install_only_and_requires_existing_risk_consent() {
        let flag = "--overwrite-managed-scripts";
        assert_eq!(options(&args(&["install", ACCEPT, flag])).unwrap(), (Action::Install, false, true));
        for a in [vec!["install", flag], vec!["inspect", flag], vec!["verify", flag], vec!["install", ACCEPT, flag, flag]] {
            assert!(options(&args(&a)).is_err());
        }
        assert!(script("0123456789abcdef", &Action::Verify, true).is_err());
        for overwrite in [false, true] {
            let (_, _, payload) = script("0123456789abcdef", &Action::Install, overwrite).unwrap();
            assert!(String::from_utf8(payload).unwrap().contains(if overwrite {"OVERWRITE=1"} else {"OVERWRITE=0"}));
        }
    }
    #[test]
    fn consent_and_unknown_arguments() {
        assert!(arguments(&args(&["install"])).is_err());
        assert!(arguments(&args(&["install","--force"])).is_err());
        assert!(arguments(&args(&["verify",ACCEPT])).is_err());
        assert_eq!(arguments(&args(&["install",ACCEPT])).unwrap(),Action::Install);
        assert_eq!(arguments(&args(&["inspect"])).unwrap(),Action::Inspect);
    }
    #[test]
    fn payload_is_fixed_bounded_and_distinct_per_action() {
        for action in [Action::Install,Action::Verify] {
            let (path,launch,data)=script("0123456789abcdef",&action,false).unwrap();
            assert!(path.starts_with("/tmp/.cc2-hid-"));
            assert!(!launch.contains(' ')); assert!(launch.len()<=127);
            assert!(data.len()<32768); assert!(!data.windows(7).any(|w|w==b"@TOKEN@"));
        }
        assert!(script("../../x;evil",&Action::Install,false).is_err());
    }
    #[test]
    fn never_accept_stale_or_wrong_operation_success() {
        assert!(!terminal(b"other:DONE","0123456789abcdef",&Action::Install).unwrap());
        assert!(terminal(b"0123456789abcdef:DONE","0123456789abcdef",&Action::Install).unwrap());
        assert!(terminal(b"0123456789abcdef:DONE","0123456789abcdef",&Action::Verify).is_err());
        for status in ["FAIL","PART","unknown"] {
            assert!(terminal(format!("0123456789abcdef:{status}").as_bytes(),"0123456789abcdef",&Action::Install).is_err());
        }
    }
    struct VersionReply { status: u32, payload: Vec<u8>, calls: usize }
    impl protocol::Transport for VersionReply {
        fn exchange(&mut self, request: &[u8; protocol::SIZE]) -> Result<Vec<u8>> {
            assert_eq!(*request, protocol::report(1, &[], 1, 0)?,
                "diagnostics must never send an upload or other camera command");
            self.calls += 1;
            Ok(protocol::report(1, &self.payload, 1, self.status)?.to_vec())
        }
    }
    #[test]
    fn version_diagnostics_preserve_refusals_and_send_only_one_read_query() {
        for (status, payload, reason) in [
            (2, vec![], "nonzero status"),
            (1, b"unexpected".to_vec(), "nonzero status"),
            (0, vec![], "empty version payload"),
            (0, vec![b'x'; 24], "exceeds 23 bytes"),
            (0, vec![b'A', 0, 10, 13, 27, 255], "non-printable bytes"),
        ] {
            let mut t = VersionReply { status, payload: payload.clone(), calls: 0 };
            let error = query_version(&mut t).unwrap_err().to_string();
            assert!(error.contains(reason), "{error}");
            assert!(error.contains(&format!("status={status} (0x{status:08x})")));
            assert!(error.contains(&format!("payload_len={}", payload.len())));
            assert!(error.contains("escaped=\""));
            assert!(error.contains("hex=["));
            assert!(error.contains("No upload was started."));
            assert!(error.bytes().all(|b| (0x20..=0x7e).contains(&b)));
            assert_eq!(t.calls, 1);
        }
        let mut t = VersionReply { status: 0, payload: b"1.0.30B".to_vec(), calls: 0 };
        assert_eq!(query_version(&mut t).unwrap(), Some(b"1.0.30B".to_vec()));
        assert_eq!(t.calls, 1);
    }
    #[test]
    fn unavailable_version_is_metadata_only_and_sends_one_read_query() {
        let mut t = VersionReply { status: 1, payload: vec![], calls: 0 };
        assert_eq!(query_version(&mut t).unwrap(), None);
        assert_eq!(t.calls, 1);
        assert!(version_unavailable(1, &[]));
        assert!(!version_unavailable(0, &[]));
        assert!(!version_unavailable(2, &[]));
        assert!(!version_unavailable(1, b"unexpected"));
    }
    #[test]
    fn diagnostic_preview_is_bounded_and_control_bytes_are_escaped() {
        let mut t = VersionReply { status: 0, payload: vec![0, 10, 13, 27, 255], calls: 0 };
        let error = query_version(&mut t).unwrap_err().to_string();
        assert!(error.contains(r#"escaped="\x00\n\r\x1b\xff""#), "{error}");
        assert!(error.contains("hex=[00, 0a, 0d, 1b, ff]"), "{error}");
        let mut t = VersionReply { status: 0, payload: vec![255; protocol::CHUNK], calls: 0 };
        let error = query_version(&mut t).unwrap_err().to_string();
        assert!(error.contains("payload_len=1010"));
        assert!(error.contains("preview truncated to 64 bytes"));
        assert!(error.len() < 1024);
        assert_eq!(t.calls, 1);
    }

}
