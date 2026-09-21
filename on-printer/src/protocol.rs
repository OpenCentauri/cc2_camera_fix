//! Independent implementation of the normal-mode HID wire format.
use crate::Result;
pub const SIZE: usize = 1024;
pub const CHUNK: usize = SIZE - 14;

pub fn crc(data: impl IntoIterator<Item = u8>) -> u16 {
    let mut value = 0xffffu16;
    for byte in data {
        value ^= byte as u16;
        for _ in 0..8 {
            value = if value & 1 != 0 { (value >> 1) ^ 0x1021 } else { value >> 1 };
        }
    }
    value
}

pub fn report(command: u16, payload: &[u8], kind: u8, sequence: u32) -> Result<[u8; SIZE]> {
    if payload.len() > CHUNK || !matches!(kind, 1 | 2) { return Err("invalid outgoing frame".into()); }
    let mut r = [0; SIZE];
    r[..3].copy_from_slice(&[1, 0x5a, 0x5a]);
    r[3..5].copy_from_slice(&command.to_le_bytes());
    r[5] = kind;
    r[6..8].copy_from_slice(&(payload.len() as u16).to_le_bytes());
    r[8..12].copy_from_slice(&sequence.to_le_bytes());
    r[14..14 + payload.len()].copy_from_slice(payload);
    let checksum = crc(r[..12].iter().chain(payload).copied());
    r[12..14].copy_from_slice(&checksum.to_le_bytes());
    Ok(r)
}

pub fn response(r: &[u8], command: u16) -> Result<(u32, Vec<u8>)> {
    if r.len() < 14 || r.len() > SIZE || r[..3] != [1, 0x5a, 0x5a] {
        return Err("invalid HID response header or length".into());
    }
    if u16::from_le_bytes([r[3], r[4]]) != command || r[5] > 2 {
        return Err("unexpected HID response command/type".into());
    }
    let n = u16::from_le_bytes([r[6], r[7]]) as usize;
    if n > CHUNK || n + 14 > r.len() || (r[5] == 0 && n != 0) {
        return Err("invalid HID response payload length".into());
    }
    let payload = &r[14..14+n];
    if crc(r[..12].iter().chain(payload).copied()) != u16::from_le_bytes([r[12], r[13]]) {
        return Err("HID response CRC mismatch".into());
    }
    Ok((u32::from_le_bytes(r[8..12].try_into().unwrap()), payload.to_vec()))
}

pub trait Transport {
    fn exchange(&mut self, request: &[u8; SIZE]) -> Result<Vec<u8>>;
}

/// Logs complete HID reports before decoding, without adding exchanges or retries.
pub struct Trace<T, W> {
    pub inner: T,
    pub writer: W,
    pub enabled: bool,
}
fn dump(writer: &mut impl std::io::Write, direction: &str, bytes: &[u8]) -> std::io::Result<()> {
    writeln!(writer, "HID {direction} len={}", bytes.len())?;
    for (i, chunk) in bytes.chunks(16).enumerate() {
        write!(writer, "{:04x}:", i * 16)?;
        for byte in chunk { write!(writer, " {byte:02x}")?; }
        writeln!(writer)?;
    }
    writer.flush()
}
impl<T: Transport, W: std::io::Write> Transport for Trace<T, W> {
    fn exchange(&mut self, request: &[u8; SIZE]) -> Result<Vec<u8>> {
        if self.enabled { dump(&mut self.writer, "TX attempt", request)?; }
        let result = self.inner.exchange(request);
        if self.enabled {
            match &result {
                Ok(reply) => dump(&mut self.writer, "RX", reply)?,
                Err(error) => {
                    writeln!(self.writer, "HID transport error: {:?}", error.to_string())?;
                    self.writer.flush()?;
                }
            }
        }
        result
    }
}

pub fn send(t: &mut impl Transport, command: u16, data: &[u8], kind: u8, seq: u32) -> Result<(u32, Vec<u8>)> {
    response(&t.exchange(&report(command, data, kind, seq)?)?, command)
}
fn checked(t: &mut impl Transport, command: u16, data: &[u8], kind: u8, seq: u32) -> Result<()> {
    let (status, _) = send(t, command, data, kind, seq)?;
    if status != 0 { return Err(format!("camera rejected 0x{command:04x}: status {status}").into()); }
    Ok(())
}

/// The sole command-injection target is generated internally, never caller supplied.
pub fn upload(t: &mut impl Transport, path: &[u8], data: &[u8], launch: bool) -> Result<()> {
    if path.is_empty() || path.len() > 127 || path.contains(&0) || path.contains(&b' ')
        || data.is_empty() || data.len() > 32768 {
        return Err("invalid bounded upload".into());
    }
    // No retries: wrong-state commands can receive misleading status-zero replies.
    checked(t, 0x3000, &[], 1, 0)?;
    checked(t, 0x3110, path, 1, 0)?;
    let count = data.chunks(CHUNK).len();
    for (i, chunk) in data.chunks(CHUNK).enumerate() {
        checked(t, 0x3200, chunk, if i+1 == count {2} else {1}, i as u32)?;
    }
    let (status, _) = send(t, 0x3300, &[], 1, 0)?;
    if status != if launch {1} else {0} {
        return Err("unexpected upload commit status; installation outcome unknown".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn trace_preserves_full_reports_and_errors_without_extra_exchanges() {
        struct Reply { bytes: Vec<u8>, calls: usize, fail: bool }
        impl Transport for Reply {
            fn exchange(&mut self, _: &[u8; SIZE]) -> Result<Vec<u8>> {
                self.calls += 1;
                if self.fail { Err("injected failure".into()) } else { Ok(self.bytes.clone()) }
            }
        }
        for enabled in [false, true] {
            for fail in [false, true] {
                // Deliberately malformed: trace must include bytes before decoding.
                let bytes = vec![0xff, 0, 0x1b];
                let mut t = Trace { inner: Reply { bytes, calls: 0, fail }, writer: Vec::new(), enabled };
                assert!(send(&mut t, 1, &[], 1, 0).is_err());
                assert_eq!(t.inner.calls, 1);
                let log = String::from_utf8(t.writer).unwrap();
                if enabled {
                    assert!(log.contains("HID TX attempt len=1024"));
                    assert!(log.contains("03f0:"), "padding must not be omitted");
                    assert!(log.contains(if fail { "injected failure" } else { "HID RX len=3\n0000: ff 00 1b" }));
                    assert!(!log.contains('\x1b'));
                } else { assert!(log.is_empty()); }
            }
        }
    }
    #[test]
    fn frames_and_corruption() {
        assert_eq!(crc(b"123456789".iter().copied()), 0x1dba);
        let r = report(0x3200, b"abc", 2, 7).unwrap();
        assert_eq!(response(&r, 0x3200).unwrap(), (7, b"abc".to_vec()));
        for i in 0..17 {
            let mut bad = r; bad[i] ^= 1;
            assert!(response(&bad, 0x3200).is_err());
        }
        assert!(response(&r[..16], 0x3200).is_err());
        assert!(report(1, &[0; 1011], 1, 0).is_err());
    }
    struct Mock { commands: Vec<u16>, fail: Option<usize>, launch: bool }
    impl Transport for Mock {
        fn exchange(&mut self, r: &[u8; SIZE]) -> Result<Vec<u8>> {
            let cmd = u16::from_le_bytes([r[3], r[4]]);
            self.commands.push(cmd);
            if self.fail == Some(self.commands.len()) { return Err("injected transport failure".into()); }
            Ok(report(cmd, &[], 1, u32::from(self.launch && cmd==0x3300))?.to_vec())
        }
    }
    #[test]
    fn failures_never_advance_or_retry() {
        for fail in 1..=5 {
            let mut t = Mock { commands: vec![], fail: Some(fail), launch: false };
            assert!(upload(&mut t, b"/tmp/file", &[1; 1011], false).is_err());
            assert_eq!(t.commands.len(), fail);
        }
        let mut t = Mock { commands: vec![], fail: None, launch: false };
        assert!(upload(&mut t, b"/tmp/file", &[], false).is_err());
        assert!(t.commands.is_empty());
        upload(&mut t, b"/tmp/file", &[1; 1011], false).unwrap();
        assert_eq!(t.commands, [0x3000, 0x3110, 0x3200, 0x3200, 0x3300]);
        t.launch = true;
        upload(&mut t, b"/tmp/launch", b"\n", true).unwrap();
    }
}
