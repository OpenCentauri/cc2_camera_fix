//! Linux usbfs ABI, restricted to the selected HID interface. No USB reset,
//! configuration changes, generic ioctl entry point, or video-driver detachment.
use std::{fs::{self, File, OpenOptions}, io::Read, os::{fd::AsRawFd, raw::{c_int, c_ulong, c_void}}, path::Path};
use crate::{Result, protocol::{Transport, SIZE}};

#[cfg(not(all(target_os = "linux", any(target_arch = "arm", target_arch = "x86_64", target_arch = "aarch64"))))]
compile_error!("usbfs ioctl layout is supported only on Linux ARM, AArch64 and x86_64");

#[derive(Debug, Clone, PartialEq)]
pub struct Interface { pub number: u8, pub input: u8, pub output: Option<u8> }
#[derive(Debug)]
pub struct Camera { pub path: String, pub interface: Interface, pub descriptors: Vec<u8> }

fn read_bounded(path: &Path, limit: usize) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    File::open(path)?.take((limit+1) as u64).read_to_end(&mut data)?;
    if data.len()>limit { return Err("oversized USB sysfs attribute".into()); }
    Ok(data)
}
fn number(path: &Path, radix: u32) -> Result<u16> {
    Ok(u16::from_str_radix(std::str::from_utf8(&read_bounded(path, 32)?)?.trim(), radix)?)
}

pub fn interface(data: &[u8], active: u8) -> Result<Interface> {
    if data.len()<18 || data[0..2] != [18,1] || data[8..12] != [0x08,0xa1,0x40,0x22] {
        return Err("not the supported normal-mode camera USB descriptor".into());
    }
    let mut candidates = Vec::new();
    let mut offset = 18;
    while offset < data.len() {
        if offset+9 > data.len() || data[offset..offset+2] != [9,2] { return Err("invalid USB configuration".into()); }
        let total = u16::from_le_bytes([data[offset+2], data[offset+3]]) as usize;
        if total<9 || offset+total>data.len() { return Err("truncated USB configuration".into()); }
        let end = offset+total;
        let selected = data[offset+5] == active;
        offset += 9;
        let mut current: Option<(Interface, u8, u8)> = None;
        while offset < end {
            if offset+2>end { return Err("truncated USB descriptor".into()); }
            let n = data[offset] as usize;
            if n<2 || offset+n>end { return Err("invalid USB descriptor length".into()); }
            let d = &data[offset..offset+n];
            if d[1]==4 {
                if let Some(c) = current.take() { candidates.push(c); }
                if n!=9 { return Err("invalid interface descriptor".into()); }
                if selected && d[5]==3 {
                    if d[3]!=0 { return Err("HID alternate settings are unsupported".into()); }
                    current = Some((Interface {number:d[2], input:0, output:None}, d[4], 0));
                }
            } else if d[1]==5 {
                if let Some((ref mut i, _, ref mut seen)) = current {
                    if n<7 || d[3] & 3 != 3 || d[2] & 0x0f == 0 || d[2] & 0x70 != 0 {
                        return Err("expected interrupt HID endpoint".into());
                    }
                    *seen += 1;
                    if d[2]&0x80 != 0 {
                        if i.input!=0 { return Err("multiple HID input endpoints".into()); }
                        i.input=d[2];
                    } else if i.output.replace(d[2]).is_some() { return Err("multiple HID output endpoints".into()); }
                }
            }
            offset += n;
        }
        if let Some(c)=current { candidates.push(c); }
    }
    if candidates.len()!=1 { return Err("expected exactly one HID interface".into()); }
    let (i, expected, seen)=candidates.remove(0);
    if i.input==0 || expected!=seen { return Err("missing HID endpoints".into()); }
    Ok(i)
}

/// Observed USB identity only; never authorizes writes to a new firmware family.
#[derive(Debug)]
pub enum Discovery {
    Hid(Camera),
    Newer30d,
}

// Device/configuration and standard descriptors from the observed 30D topology.
// Class-specific UVC payloads are deliberately not a firmware fingerprint.
const D30_HEADER: &[u8] = &[
    18, 1, 0, 2, 0xef, 2, 1, 64, 8, 0xa1, 0x40, 0x22, 0x14, 4, 1, 2, 3, 1,
    9, 2, 0xdb, 3, 4, 1, 4, 0xc0, 1,
];
const D30_TOPOLOGY: &[&[u8]] = &[
    &[8, 11, 0, 2, 14, 3, 0, 5],
    &[9, 4, 0, 0, 0, 14, 1, 0, 5],
    &[9, 4, 1, 0, 0, 14, 2, 0, 6],
    &[9, 4, 1, 1, 1, 14, 2, 0, 6],
    &[7, 5, 0x81, 5, 0xfc, 3, 1],
    &[8, 11, 2, 2, 14, 3, 0, 8],
    &[9, 4, 2, 0, 0, 14, 1, 0, 8],
    &[9, 4, 3, 0, 0, 14, 2, 0, 9],
    &[9, 4, 3, 1, 1, 14, 2, 0, 9],
    &[7, 5, 0x82, 5, 0xfc, 3, 1],
];
fn observed_30d(data: &[u8], active: u8, manufacturer: &str, product: &str) -> bool {
    if active != 1 || manufacturer != "Linux Foundation"
        || product != "Multi Composite Double Uvc Gadget"
        || data.len() != 0x3ed || !data.starts_with(D30_HEADER) {
        return false;
    }
    let mut offset = D30_HEADER.len();
    let mut matched = 0;
    while offset < data.len() {
        if offset + 2 > data.len() { return false; }
        let n = data[offset] as usize;
        if n < 3 || offset + n > data.len() { return false; }
        let descriptor = &data[offset..offset+n];
        if descriptor[1] != 0x24 {
            if D30_TOPOLOGY.get(matched).copied() != Some(descriptor) { return false; }
            matched += 1;
        }
        offset += n;
    }
    matched == D30_TOPOLOGY.len()
}

struct Candidate {
    path: String,
    active: u8,
    descriptors: Vec<u8>,
    manufacturer: String,
    product: String,
}
fn classify(mut candidates: Vec<Candidate>) -> Result<Discovery> {
    // Count before classification: a 30D alongside a 30B must not hide ambiguity.
    if candidates.len() != 1 {
        return Err(format!("expected one camera with the stock USB ID, found {}", candidates.len()).into());
    }
    let c = candidates.remove(0);
    if observed_30d(&c.descriptors, c.active, &c.manufacturer, &c.product) {
        return Ok(Discovery::Newer30d);
    }
    Ok(Discovery::Hid(Camera {
        path: c.path,
        interface: interface(&c.descriptors, c.active)?,
        descriptors: c.descriptors,
    }))
}
fn optional_string(path: &Path) -> Result<String> {
    match read_bounded(path, 1024) {
        Ok(data) => Ok(std::str::from_utf8(&data)?.trim_end_matches('\n').to_owned()),
        Err(e) if e.downcast_ref::<std::io::Error>().is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) => Ok(String::new()),
        Err(e) => Err(e),
    }
}
pub fn discover() -> Result<Discovery> {
    let mut cameras = Vec::new();
    for entry in fs::read_dir("/sys/bus/usb/devices")? {
        let p = entry?.path();
        if !p.join("idVendor").exists() { continue; }
        let vid = number(&p.join("idVendor"),16)?;
        let pid = number(&p.join("idProduct"),16)?;
        if vid != 0xa108 || !matches!(pid, 0x2240 | 0xff08) { continue; }
        if pid==0xff08 { return Err("camera is in bootloader mode; this installer requires a working camera".into()); }
        let bus = number(&p.join("busnum"),10)?;
        let dev = number(&p.join("devnum"),10)?;
        if bus==0 || bus>999 || dev==0 || dev>127 { return Err("invalid USB bus/address".into()); }
        cameras.push(Candidate {
            path: format!("/dev/bus/usb/{bus:03}/{dev:03}"),
            active: u8::try_from(number(&p.join("bConfigurationValue"),10)?)?,
            descriptors: read_bounded(&p.join("descriptors"), 65536)?,
            manufacturer: optional_string(&p.join("manufacturer"))?,
            product: optional_string(&p.join("product"))?,
        });
    }
    classify(cameras)
}

#[repr(C)]
struct Bulk { ep:u32, len:u32, timeout:u32, data:*mut c_void }
#[repr(C)]
struct Control { request_type:u8, request:u8, value:u16, index:u16, length:u16, timeout:u32, data:*mut c_void }
#[repr(C)]
struct Driver { interface:u32, name:[u8;256] }
#[repr(C)]
struct Disconnect { interface:u32, flags:u32, driver:[u8;256] }
#[repr(C)]
struct InterfaceIoctl { interface:c_int, code:c_int, data:*mut c_void }
extern "C" { fn ioctl(fd:c_int, request:c_ulong, ...) -> c_int; }
const fn ioc<T>(direction:u32, n:u32) -> c_ulong {
    ((direction<<30) | ((std::mem::size_of::<T>() as u32)<<16) | (0x55<<8) | n) as c_ulong
}
fn call<T>(file:&File, request:c_ulong, arg:&mut T) -> std::io::Result<usize> {
    // All callers supply a matching repr(C) ABI object; buffers outlive the
    // synchronous ioctl. No asynchronous URB points into Rust-managed memory.
    let n=unsafe { ioctl(file.as_raw_fd(), request, arg as *mut T) };
    if n<0 { Err(std::io::Error::last_os_error()) } else { Ok(n as usize) }
}

pub struct Usb { file:File, interface:Interface, detached:bool }
impl Usb {
    pub fn open(camera:Camera) -> Result<Self> {
        let mut file=OpenOptions::new().read(true).write(true).open(&camera.path)?;
        // Confirm the opened device still has the descriptors used for selection.
        let mut actual=vec![0; camera.descriptors.len()];
        file.read_exact(&mut actual)?;
        if actual!=camera.descriptors { return Err("USB device changed during selection".into()); }
        let mut driver=Driver {interface:camera.interface.number as u32, name:[0;256]};
        let bound=match call(&file,ioc::<Driver>(1,8),&mut driver) {
            Ok(_)=>true,
            Err(e) if e.raw_os_error()==Some(61)=>false, // ENODATA: no kernel driver
            Err(e)=>return Err(e.into()),
        };
        if bound {
            if driver.name[..7] != *b"usbhid\0" { return Err("refusing to detach an unexpected interface driver".into()); }
            let mut claim=Disconnect {interface:driver.interface,flags:1,driver:driver.name};
            call(&file,ioc::<Disconnect>(2,27),&mut claim)?;
        } else {
            call(&file,ioc::<u32>(2,15),&mut driver.interface)?;
        }
        Ok(Self {file, interface:camera.interface, detached:bound})
    }
    fn transfer(&self, ep:u8, data:&mut [u8]) -> Result<usize> {
        let mut request=Bulk {ep:ep as u32,len:data.len() as u32,timeout:5000,data:data.as_mut_ptr().cast()};
        // usb_bulk_msg also accepts interrupt endpoints (Linux message.c).
        Ok(call(&self.file,ioc::<Bulk>(3,2),&mut request)?)
    }
}
impl Transport for Usb {
    fn exchange(&mut self, report:&[u8;SIZE]) -> Result<Vec<u8>> {
        let mut data=*report;
        let count=if let Some(ep)=self.interface.output { self.transfer(ep,&mut data)? } else {
            let mut request=Control {request_type:0x21,request:9,value:0x0201,index:self.interface.number as u16,length:SIZE as u16,timeout:5000,data:data.as_mut_ptr().cast()};
            call(&self.file,ioc::<Control>(3,0),&mut request)?
        };
        if count!=SIZE { return Err("short HID write; no retry performed".into()); }
        let mut response=[0;SIZE];
        let n=self.transfer(self.interface.input,&mut response)?;
        Ok(response[..n].to_vec())
    }
}
impl Drop for Usb {
    fn drop(&mut self) {
        let mut i=self.interface.number as u32;
        if let Err(e)=call(&self.file,ioc::<u32>(2,16),&mut i) { eprintln!("HID interface release failed: {e}"); }
        if self.detached {
            let mut request=InterfaceIoctl {interface:i as c_int,code:0x5517,data:std::ptr::null_mut()};
            if let Err(e)=call(&self.file,ioc::<InterfaceIoctl>(3,18),&mut request) { eprintln!("HID driver reattachment failed: {e}"); }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn descriptors() -> Vec<u8> {
        let mut d=vec![18,1,0,2,0,0,0,64,8,0xa1,0x40,0x22,0,1,0,0,0,1];
        d.extend_from_slice(&[9,2,32,0,1,1,0,0x80,50, 9,4,2,0,2,3,0,0,0, 7,5,0x83,3,0,4,1, 7,5,4,3,0,4,1]); d
    }
    #[test]
    fn descriptor_selection_and_refusals() {
        let d=descriptors();
        assert_eq!(interface(&d,1).unwrap(),Interface {number:2,input:0x83,output:Some(4)});
        for n in 0..d.len() { assert!(interface(&d[..n],1).is_err()); }
        assert!(interface(&d,2).is_err());
        for (i,v) in [(8,0),(30,1),(32,14),(36,0),(39,2),(38,3),(45,0x84)] {
            let mut bad=d.clone(); bad[i]=v; assert!(interface(&bad,1).is_err(),"offset {i}");
        }
    }
    fn synthetic_30d() -> Candidate {
        let mut data = D30_HEADER.to_vec();
        for d in D30_TOPOLOGY { data.extend_from_slice(d); }
        // Synthetic class-specific payloads: the classifier checks the standard
        // USB topology, not the contents of video format/frame declarations.
        while data.len() < 0x3ed {
            let n = (0x3ed - data.len()).min(250);
            assert!(n >= 3);
            data.extend_from_slice(&[n as u8, 0x24, 0]);
            data.resize(data.len() + n - 3, 0);
        }
        Candidate {
            path: "/must/not/be/opened".into(), active: 1, descriptors: data,
            manufacturer: "Linux Foundation".into(),
            product: "Multi Composite Double Uvc Gadget".into(),
        }
    }
    #[test]
    fn newer_camera_is_classified_from_sysfs_only() {
        assert!(matches!(classify(vec![synthetic_30d()]).unwrap(), Discovery::Newer30d));
        assert!(classify(vec![]).is_err());
        assert!(classify(vec![synthetic_30d(), synthetic_30d()]).is_err());
        let old = Candidate { descriptors: descriptors(), ..synthetic_30d() };
        assert!(matches!(classify(vec![old]).unwrap(), Discovery::Hid(_)));
        let old = Candidate { descriptors: descriptors(), ..synthetic_30d() };
        assert!(classify(vec![old, synthetic_30d()]).is_err());
    }
    #[test]
    fn shared_ids_missing_hid_and_partial_matches_do_not_identify_30d() {
        for offset in 0..111 {
            let mut c = synthetic_30d();
            c.descriptors[offset] ^= 1;
            assert!(classify(vec![c]).is_err(), "changed standard descriptor byte {offset}");
        }
        for n in [0, 18, 27, 111, 1004] {
            let mut c = synthetic_30d(); c.descriptors.truncate(n);
            assert!(classify(vec![c]).is_err());
        }
        let mut c = synthetic_30d(); c.descriptors[111] = 0;
        assert!(classify(vec![c]).is_err());
        let mut c = synthetic_30d(); c.product = "USB Camera".into();
        assert!(classify(vec![c]).is_err());
        let mut c = synthetic_30d(); c.manufacturer.clear();
        assert!(classify(vec![c]).is_err());
        let mut c = synthetic_30d(); c.active = 2;
        assert!(classify(vec![c]).is_err());
    }
    #[test]
    fn ioctl_abi() {
        assert_eq!(ioc::<u32>(2,15),0x8004550f);
        assert_eq!(ioc::<Disconnect>(2,27),0x8108551b);
        assert_eq!(ioc::<Bulk>(3,2),if cfg!(target_pointer_width="64") {0xc0185502} else {0xc0105502});
    }
}
