use libloading::Library;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::ptr;

type Compatible = unsafe extern "C" fn(c_int, c_int) -> c_int;
type Version = unsafe extern "C" fn() -> *const c_char;
type Fetch = unsafe extern "C" fn(*const c_char, *const c_char, *const c_char, c_int) -> *mut c_void;
type Json = unsafe extern "C" fn(*const c_void, *mut c_char, usize) -> c_int;
type ExitCode = unsafe extern "C" fn(*const c_void) -> c_int;
type Free = unsafe extern "C" fn(*mut c_void);
type Manifest = unsafe extern "C" fn(*mut c_char, usize) -> c_int;

pub struct NativeRuntime {
    _library: Library,
    version: Version,
    fetch: Fetch,
    json: Json,
    exit_code: ExitCode,
    free: Free,
    manifest: Manifest,
}

pub struct Snapshot<'a> {
    handle: *mut c_void,
    runtime: &'a NativeRuntime,
}

impl Drop for Snapshot<'_> {
    fn drop(&mut self) {
        // SAFETY: fetch returned this handle, and the library outlives this borrow.
        unsafe { (self.runtime.free)(self.handle) };
    }
}

fn library_path() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("WAVR_NATIVE_LIB") {
        if path.is_empty() {
            return Err("WAVR_NATIVE_LIB is empty".into());
        }
        return Ok(PathBuf::from(path));
    }
    #[cfg(target_os = "windows")]
    const NAMES: &[&str] = &["libwavr_native.dll", "wavr_native.dll"];
    #[cfg(target_os = "linux")]
    const NAMES: &[&str] = &["libwavr_native.so"];
    #[cfg(not(any(target_os = "windows", target_os = "linux")))]
    compile_error!("This client supports Windows and Linux only");
    let directory = std::env::current_exe()
        .map_err(|e| format!("Cannot locate executable: {e}"))?
        .parent()
        .ok_or("Executable has no parent directory")?
        .to_path_buf();
    for name in NAMES {
        let candidate = directory.join(name);
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    Err(format!("Wavr native library not found next to {} (expected {}). Set WAVR_NATIVE_LIB to its path.", directory.display(), NAMES.join(" or ")))
}

impl NativeRuntime {
    pub fn load() -> Result<Self, String> {
        let path = library_path()?;
        Self::load_from(&path)
    }

    pub fn load_from(path: &Path) -> Result<Self, String> {
        // SAFETY: Symbol signatures match wavr.h ABI 1.1. The library is retained in Self.
        unsafe {
            let library = Library::new(path)
                .map_err(|e| format!("Cannot load Wavr native library {}: {e}", path.display()))?;
            let compatible: Compatible = *library.get(b"wavr_abi_compatible\0")
                .map_err(|e| format!("Wavr native library lacks ABI 1.1 compatibility check: {e}"))?;
            if compatible(1, 1) == 0 {
                return Err("Wavr native library is incompatible with ABI 1.1".into());
            }
            macro_rules! symbol {
                ($name:literal, $ty:ty) => {
                    *library.get::<$ty>(concat!($name, "\0").as_bytes())
                        .map_err(|e| format!("Wavr native library lacks {}: {e}", $name))?
                };
            }
            let version = symbol!("wavr_version", Version);
            let fetch = symbol!("wavr_snapshot_fetch", Fetch);
            let json = symbol!("wavr_snapshot_json", Json);
            let exit_code = symbol!("wavr_snapshot_exit_code", ExitCode);
            let free = symbol!("wavr_snapshot_free", Free);
            let manifest = symbol!("wavr_capability_manifest", Manifest);
            Ok(Self { _library: library, version, fetch, json, exit_code, free, manifest })
        }
    }

    pub fn version(&self) -> Option<String> {
        // SAFETY: ABI specifies a static NUL-terminated string or NULL.
        let value = unsafe { (self.version)() };
        if value.is_null() { return None; }
        unsafe { CStr::from_ptr(value).to_str().ok().map(str::to_owned) }
    }

    pub fn fetch(&self, url: &str, token: Option<&str>, pin: Option<&str>) -> Result<Snapshot<'_>, String> {
        let url = CString::new(url).map_err(|_| "URL contains a NUL byte")?;
        let token = token.map(CString::new).transpose().map_err(|_| "Token contains a NUL byte")?;
        let pin = pin.map(CString::new).transpose().map_err(|_| "PIN contains a NUL byte")?;
        // SAFETY: all pointers remain valid for the call. NULL optional arguments are permitted.
        let handle = unsafe { (self.fetch)(url.as_ptr(), token.as_ref().map_or(ptr::null(), |v| v.as_ptr()), pin.as_ref().map_or(ptr::null(), |v| v.as_ptr()), 4000) };
        if handle.is_null() {
            Err("Wavr native library could not allocate a snapshot".into())
        } else {
            Ok(Snapshot { handle, runtime: self })
        }
    }

    pub fn capability_manifest(&self) -> Result<String, String> {
        self.read_json(|out, len| unsafe { (self.manifest)(out, len) })
    }

    fn read_json(&self, call: impl Fn(*mut c_char, usize) -> c_int) -> Result<String, String> {
        let needed = call(ptr::null_mut(), 0);
        if !(0..=16_777_216).contains(&needed) {
            return Err(format!("Native JSON length is invalid: {needed}"));
        }
        let mut buffer = vec![0u8; needed as usize + 1];
        let written = call(buffer.as_mut_ptr().cast(), buffer.len());
        if written < 0 || written as usize >= buffer.len() || buffer[written as usize] != 0 {
            return Err("Native JSON buffer changed or was not NUL terminated".into());
        }
        buffer.truncate(written as usize);
        String::from_utf8(buffer).map_err(|_| "Native JSON is not UTF-8".into())
    }
}

impl Snapshot<'_> {
    pub fn json(&self) -> Result<String, String> {
        self.runtime.read_json(|out, len| unsafe { (self.runtime.json)(self.handle, out, len) })
    }

    pub fn exit_code(&self) -> i32 {
        // SAFETY: handle is live until Drop.
        unsafe { (self.runtime.exit_code)(self.handle) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_library_has_clear_error() {
        let missing = std::env::temp_dir().join("wavr-missing-abi-1-1-library.dll");
        let error = NativeRuntime::load_from(&missing).err().expect("loading must fail");
        assert!(error.contains("Cannot load Wavr native library"));
        assert!(error.contains("wavr-missing-abi-1-1-library.dll"));
    }

    /// Against the real library when WAVR_NATIVE_LIB names one (CI and the
    /// release check set it): the ABI handshake, the version, and a Core that
    /// does not answer arriving as a snapshot with exit code 2 -- never a panic.
    #[test]
    fn real_library_binds_and_reports_an_absent_core() {
        let Ok(path) = std::env::var("WAVR_NATIVE_LIB") else { return };
        let rt = NativeRuntime::load_from(Path::new(&path)).expect("the library must load");
        assert!(rt.version().is_some_and(|v| !v.is_empty()));
        let snap = rt.fetch("http://127.0.0.1:9", None, None).expect("a snapshot, not an error");
        assert_eq!(snap.exit_code(), 2);
        let json = snap.json().expect("snapshot json");
        let parsed = crate::snapshot::Snapshot::parse(&json).expect("parses");
        assert_eq!(parsed.reachable, Some(false));
        assert!(rt.capability_manifest().expect("manifest").contains("\"protocol_version\":1"));
    }
}
