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
      Read USB descriptors and query the camera version. No camera file writes.
  cc2camera-hid install --accept-no-backup-space-risk
      Install the persistent erase-fix hook through HID. NO BACKUP is exported,
      and safe clean space is NOT checked. A failed write or power loss can leave
      the camera unbootable and require an external SPI programmer to recover.
      Firmware checks and installed-file comparisons run on the camera.
  cc2camera-hid verify
      After a successful installation and printer restart, upload a temporary
      probe and verify the installed hooks and live RAM correction. No persistent
      file or RAM correction writes; temporary files/status are written in /tmp.

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
fn script(token: &str, action: &Action) -> Result<(String, String, Vec<u8>)> {
    if token.len()!=16 || !token.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()) {
        return Err("invalid local session token".into());
    }
    let mode=match action { Action::Install=>"install", Action::Verify=>"verify", _=>return Err("invalid script action".into()) };
    let path=format!("/tmp/.cc2-hid-{token}.sh");
    // Literal fopen must fail (the embedded /bin/sh makes a nonexistent directory
    // component). Only the preceding shell's rm command launches our worker.
    let launch=format!("/tmp/.cc2-launch-{token};/bin/sh${{IFS}}{path}&");
    let data=include_str!("../payload/installer.sh").replace("@TOKEN@",token).replace("@MODE@",mode).into_bytes();
    if launch.len()>127 || data.len()>32768 { return Err("embedded installer exceeds protocol limits".into()); }
    Ok((path,launch,data))
}
fn terminal(payload: &[u8], token: &str, action: &Action) -> Result<bool> {
    let prefix=format!("{token}:");
    if !payload.starts_with(prefix.as_bytes()) { return Ok(false); }
    match &payload[prefix.len()..] {
        b"BUSY" => Ok(false),
        b"DONE" | b"SAME" if *action==Action::Install => Ok(true),
        b"LIVE" if *action==Action::Verify => Ok(true),
        b"FAIL" => Err("camera-side checks refused the operation; no persistent installation writes were started. Do not retry this boot.".into()),
        b"PART" => Err("installation failed after persistent writes began; camera may be unbootable. Do not retry or restart blindly; preserve power and seek recovery using the documented backup/programmer route.".into()),
        _ => Err("unexpected camera operation status".into()),
    }
}
fn run() -> Result<()> {
    // Parse consent and prepare every outgoing target before touching USB.
    let action=arguments(&std::env::args().skip(1).collect::<Vec<_>>())?;
    if action==Action::Help { print!("{HELP}"); return Ok(()); }
    let mut random=[0u8;8];
    let prepared=if matches!(action,Action::Install|Action::Verify) {
        std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
        let token=format!("{:016x}", u64::from_be_bytes(random));
        let payload=script(&token,&action)?;
        Some((token,payload))
    } else { None };
    let camera=usb::discover()?;
    println!("Camera {}: HID interface {}, input 0x{:02x}, output {:?}",camera.path,camera.interface.number,camera.interface.input,camera.interface.output);
    let mut transport=usb::Usb::open(camera)?;
    let (status,version)=send(&mut transport,1,&[],1,0)?;
    if status!=0 || version.is_empty() || version.len()>23 || !version.iter().all(|b| (0x20..=0x7e).contains(b)) {
        return Err("invalid camera version response".into());
    }
    println!("Camera version: {}",String::from_utf8_lossy(&version));
    if let Some((token,(path,launch,data)))=prepared {
        if action==Action::Install { println!("Accepted: no exported backup or clean-space check; programmer recovery may be required."); }
        println!("Uploading temporary camera worker; session {token}. Do not interrupt power.");
        upload(&mut transport,path.as_bytes(),&data,false)?;
        upload(&mut transport,launch.as_bytes(),b"\n",true)?;
        let deadline=Instant::now()+Duration::from_secs(90);
        while Instant::now()<deadline {
            let (status,payload)=send(&mut transport,1,&[],1,0)?;
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
            let (path,launch,data)=script("0123456789abcdef",&action).unwrap();
            assert!(path.starts_with("/tmp/.cc2-hid-"));
            assert!(!launch.contains(' ')); assert!(launch.len()<=127);
            assert!(data.len()<32768); assert!(!data.windows(7).any(|w|w==b"@TOKEN@"));
        }
        assert!(script("../../x;evil",&Action::Install).is_err());
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
}
