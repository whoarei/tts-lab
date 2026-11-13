//! Narrow UI commands for the project's HTTPS/SHA256 update channel.
use crate::{
    desktop::{check_window, AppState},
    update_apply::ApplyRequest,
    updates::{self, DownloadedUpdate, PackageKind, UpdateCandidate},
};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    cell::Cell,
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, WebviewWindow};

#[derive(Clone, Serialize)]
pub(crate) struct UpdateView {
    status: String,
    current_version: String,
    version: Option<String>,
    notes: Option<String>,
    downloaded_bytes: u64,
    total_bytes: u64,
    error: Option<String>,
    distribution: String,
}

#[derive(Default)]
struct Payload {
    candidate: Option<UpdateCandidate>,
    download: Option<DownloadedUpdate>,
}

pub(crate) struct UpdateManager {
    view: Mutex<UpdateView>,
    payload: Mutex<Payload>,
    operation: AtomicBool,
    target: PathBuf,
    cache: PathBuf,
}

struct Operation<'a>(&'a AtomicBool);
impl Drop for Operation<'_> {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}
struct QuitFreeze<'a> {
    flag: &'a AtomicBool,
    committed: bool,
}
impl Drop for QuitFreeze<'_> {
    fn drop(&mut self) {
        if !self.committed {
            self.flag.store(false, Ordering::SeqCst);
        }
    }
}

impl UpdateManager {
    pub(crate) fn new(target: PathBuf, profile: &Path) -> Self {
        let distribution = distribution(&target);
        let manager = Self {
            view: Mutex::new(UpdateView {
                status: "idle".into(),
                current_version: env!("CARGO_PKG_VERSION").into(),
                version: None,
                notes: None,
                downloaded_bytes: 0,
                total_bytes: 0,
                error: None,
                distribution,
            }),
            payload: Mutex::new(Payload::default()),
            operation: AtomicBool::new(false),
            target,
            cache: profile.join("updates"),
        };
        manager.read_previous_result();
        manager
    }

    fn begin(&self) -> Result<Operation<'_>, String> {
        self.operation
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "更新操作正在进行，请稍候。")?;
        Ok(Operation(&self.operation))
    }

    fn snapshot(&self) -> UpdateView {
        self.view.lock().unwrap().clone()
    }

    fn publish(&self, app: &tauri::AppHandle) -> UpdateView {
        let view = self.snapshot();
        let _ = app.emit_to("main", "desktop:update-state", &view);
        view
    }

    fn error(&self, app: &tauri::AppHandle, error: String) -> String {
        {
            let mut v = self.view.lock().unwrap();
            v.status = "error".into();
            v.error = Some(error.clone());
        }
        self.publish(app);
        error
    }

    pub(crate) fn auto_check_supported(&self) -> bool {
        let view = self.snapshot();
        // Keep a failed installation's recovery message visible on restart,
        // including when the webview cannot persist the daily-check timestamp.
        view.distribution != "development" && view.error.is_none()
    }

    fn read_previous_result(&self) {
        let pointer = self.cache.join("last-request.json");
        let Ok(data) = fs::read(&pointer) else {
            return;
        };
        if data.len() > 16_384 {
            return;
        }
        let Ok(value) = serde_json::from_slice::<Value>(&data) else {
            return;
        };
        let Some(directory) = value
            .get("directory")
            .and_then(Value::as_str)
            .map(PathBuf::from)
        else {
            return;
        };
        let (Ok(root), Ok(dir)) = (self.cache.canonicalize(), directory.canonicalize()) else {
            return;
        };
        if dir.parent() != Some(root.as_path())
            || !dir
                .file_name()
                .is_some_and(|name| name.to_string_lossy().starts_with("tts-update-"))
            || fs::symlink_metadata(&directory)
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(true)
        {
            return;
        }
        let Ok(result) = fs::read(dir.join("result.json")) else {
            return;
        };
        if result.len() > 16_384 {
            return;
        }
        let Ok(result) = serde_json::from_slice::<Value>(&result) else {
            return;
        };
        let Some(status) = result.get("status").and_then(Value::as_str) else {
            return;
        };
        {
            let mut view = self.view.lock().unwrap();
            if status == "success" {
                view.status = "current".into();
                view.notes = Some(format!(
                    "已完成更新，当前版本 v{}。",
                    env!("CARGO_PKG_VERSION")
                ));
            } else {
                view.status = "error".into();
                view.error = Some(
                    result
                        .get("message")
                        .and_then(Value::as_str)
                        .filter(|m| m.len() <= 2048)
                        .unwrap_or("上次更新未完成，请重新检查更新或手动下载安装包。")
                        .to_string(),
                );
            }
        }
        // This directory was created by this updater, validated above, and the
        // old helper has normally exited before the new backend becomes ready.
        if fs::remove_dir_all(&dir).is_ok() {
            let _ = fs::remove_file(pointer);
        }
    }
}

fn distribution(target: &Path) -> String {
    if !cfg!(windows)
        || !target.join("BUILD-MANIFEST.json").is_file()
        || !target.join("resources/app/package.json").is_file()
    {
        return "development".into();
    }
    #[cfg(windows)]
    {
        if installed_location().and_then(|p| p.canonicalize().ok()) == target.canonicalize().ok() {
            return "installed".into();
        }
    }
    "portable".into()
}

#[cfg(windows)]
fn installed_location() -> Option<PathBuf> {
    use windows_sys::Win32::System::Registry::{
        RegGetValueW, HKEY_CURRENT_USER, RRF_RT_REG_SZ, RRF_SUBKEY_WOW6432KEY,
    };
    let key: Vec<_> = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\TTS Lab"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    let name: Vec<_> = "InstallLocation".encode_utf16().chain(Some(0)).collect();
    let flags = RRF_RT_REG_SZ | RRF_SUBKEY_WOW6432KEY;
    let mut length = 0;
    unsafe {
        if RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            name.as_ptr(),
            flags,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            &mut length,
        ) != 0
            || length == 0
            || length > 65536
        {
            return None;
        }
        let mut value = vec![0u16; (length as usize + 1) / 2];
        if RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            name.as_ptr(),
            flags,
            std::ptr::null_mut(),
            value.as_mut_ptr().cast(),
            &mut length,
        ) != 0
        {
            return None;
        }
        let end = value.iter().position(|c| *c == 0).unwrap_or(value.len());
        Some(PathBuf::from(String::from_utf16(&value[..end]).ok()?))
    }
}

#[tauri::command]
pub(crate) fn get_update_state(
    window: WebviewWindow,
    app: tauri::AppHandle,
) -> Result<UpdateView, String> {
    check_window(&window)?;
    Ok(app.state::<UpdateManager>().snapshot())
}

#[tauri::command]
pub(crate) async fn check_update(
    window: WebviewWindow,
    app: tauri::AppHandle,
) -> Result<UpdateView, String> {
    check_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let manager = app.state::<UpdateManager>();
        let _operation = manager.begin()?;
        if app.state::<AppState>().smoke {
            return Err("离线测试模式不连接更新服务。".into());
        }
        {
            *manager.payload.lock().unwrap() = Payload::default();
            let mut v = manager.view.lock().unwrap();
            v.status = "checking".into();
            v.error = None;
            v.version = None;
            v.notes = None;
            v.downloaded_bytes = 0;
            v.total_bytes = 0;
        }
        manager.publish(&app);
        let kind = if manager.snapshot().distribution == "installed" {
            PackageKind::Installer
        } else {
            PackageKind::Portable
        };
        match updates::check_release(env!("CARGO_PKG_VERSION"), kind) {
            Ok(candidate) => {
                let mut view = manager.view.lock().unwrap();
                if let Some(candidate) = candidate {
                    view.status = "available".into();
                    view.version = Some(candidate.version.clone());
                    view.notes = Some(candidate.notes.clone());
                    view.total_bytes = candidate.size;
                    manager.payload.lock().unwrap().candidate = Some(candidate);
                } else {
                    view.status = "current".into();
                }
                drop(view);
                Ok(manager.publish(&app))
            }
            Err(error) => Err(manager.error(&app, error)),
        }
    })
    .await
    .map_err(|_| "无法检查更新，请重试。".to_string())?
}

#[tauri::command]
pub(crate) async fn download_update(
    window: WebviewWindow,
    app: tauri::AppHandle,
) -> Result<UpdateView, String> {
    check_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let manager = app.state::<UpdateManager>();
        let _operation = manager.begin()?;
        if app.state::<AppState>().smoke || manager.snapshot().distribution == "development" {
            return Err("请使用完整 Windows 发行版下载并安装更新。".into());
        }
        let candidate = manager
            .payload
            .lock()
            .unwrap()
            .candidate
            .clone()
            .ok_or("请先检查可用更新。")?;
        let cached = {
            let payload = manager.payload.lock().unwrap();
            payload.download.as_ref().is_some_and(|download| {
                use sha2::{Digest, Sha256};
                use std::io::Read;
                if download.candidate.version != candidate.version
                    || download.candidate.kind != candidate.kind
                    || fs::metadata(&download.path).map(|m| m.len()).ok() != Some(candidate.size)
                {
                    return false;
                }
                let Ok(mut file) = fs::File::open(&download.path) else {
                    return false;
                };
                let mut hash = Sha256::new();
                let mut buffer = [0u8; 65536];
                loop {
                    match file.read(&mut buffer) {
                        Ok(0) => break,
                        Ok(n) => hash.update(&buffer[..n]),
                        Err(_) => return false,
                    }
                }
                format!("{:x}", hash.finalize()) == download.sha256
            })
        };
        if cached {
            {
                let mut v = manager.view.lock().unwrap();
                v.status = "ready".into();
                v.error = None;
                v.downloaded_bytes = candidate.size;
                v.total_bytes = candidate.size;
            }
            return Ok(manager.publish(&app));
        }
        manager.payload.lock().unwrap().download = None;
        fs::create_dir_all(&manager.cache).map_err(|_| "无法创建更新缓存，请检查用户目录权限。")?;
        {
            let mut v = manager.view.lock().unwrap();
            v.status = "downloading".into();
            v.error = None;
            v.downloaded_bytes = 0;
        }
        manager.publish(&app);
        let last = Cell::new(Instant::now() - Duration::from_secs(1));
        let result = updates::download_update(&candidate, &manager.cache, |done, total| {
            {
                let mut v = manager.view.lock().unwrap();
                v.downloaded_bytes = done;
                v.total_bytes = total;
            }
            if done == total || last.get().elapsed() >= Duration::from_millis(100) {
                last.set(Instant::now());
                manager.publish(&app);
            }
        });
        match result {
            Ok(download) => {
                manager.payload.lock().unwrap().download = Some(download);
                manager.view.lock().unwrap().status = "ready".into();
                Ok(manager.publish(&app))
            }
            Err(error) => Err(manager.error(&app, error)),
        }
    })
    .await
    .map_err(|_| "更新下载失败，请重试。".to_string())?
}

fn atomic_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let mut temp = tempfile::NamedTempFile::new_in(path.parent().ok_or("无效的更新缓存。")?)
        .map_err(|_| "无法保存更新状态。")?;
    temp.write_all(&serde_json::to_vec(value).map_err(|_| "无法保存更新状态。")?)
        .map_err(|_| "无法保存更新状态。")?;
    temp.as_file()
        .sync_all()
        .map_err(|_| "无法保存更新状态。")?;
    temp.persist(path).map_err(|_| "无法保存更新状态。")?;
    Ok(())
}

fn prepare_helper(
    download: &DownloadedUpdate,
    target: &Path,
) -> Result<std::process::Child, String> {
    let directory = download.directory.path();
    let helper = directory.join("TTS-Lab-update-helper.exe");
    fs::copy(
        std::env::current_exe().map_err(|_| "无法准备更新程序。")?,
        &helper,
    )
    .map_err(|_| "无法准备更新程序。")?;
    let request = ApplyRequest {
        schema: 1,
        kind: download.candidate.kind,
        package: download.path.clone(),
        sha256: download.sha256.clone(),
        expected_version: download.candidate.version.clone(),
        target_dir: target.to_path_buf(),
        parent_pid: std::process::id(),
        result_path: directory.join("result.json"),
        no_restart: false,
    };
    atomic_json(&directory.join("request.json"), &request)?;
    let mut command = Command::new(helper);
    command
        .args(["--apply-update"])
        .arg(directory.join("request.json"))
        .current_dir(directory)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000 | 0x00000200);
    }
    let mut child = command
        .spawn()
        .map_err(|_| "无法启动更新程序，现有应用保持运行。")?;
    let deadline = Instant::now() + Duration::from_secs(120);
    loop {
        if let Ok(bytes) = fs::read(directory.join("ready.json")) {
            if bytes.len() <= 16384
                && serde_json::from_slice::<Value>(&bytes)
                    .ok()
                    .is_some_and(|value| {
                        value.get("status").and_then(Value::as_str) == Some("ready")
                            && value.get("schema").and_then(Value::as_u64) == Some(1)
                            && value.get("version").and_then(Value::as_str)
                                == Some(download.candidate.version.as_str())
                    })
            {
                return Ok(child);
            }
        }
        match child.try_wait() {
            Ok(Some(_)) => return Err("更新准备失败，现有应用保持运行。请重试。".into()),
            Ok(None) => {}
            Err(_) => {
                let _ = fs::write(directory.join("cancel.marker"), b"cancel");
                let _ = child.kill();
                let _ = child.wait();
                return Err("无法确认更新程序状态，已取消本次更新。".into());
            }
        }
        if Instant::now() > deadline {
            let _ = fs::write(directory.join("cancel.marker"), b"cancel");
            let _ = child.kill();
            let _ = child.wait();
            return Err("更新准备超时，现有应用保持运行。请重试。".into());
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[tauri::command]
pub(crate) async fn install_update(
    window: WebviewWindow,
    app: tauri::AppHandle,
) -> Result<(), String> {
    check_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let manager = app.state::<UpdateManager>();
        let _operation = manager.begin()?;
        let host = app.state::<AppState>();
        if host.smoke || manager.snapshot().distribution == "development" {
            return Err("开发和测试模式不安装更新。".into());
        }
        if manager.snapshot().status != "ready"
            || manager.payload.lock().unwrap().download.is_none()
        {
            return Err("请先下载并校验更新。".into());
        }
        host.quitting
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "应用正在关闭或重启。")?;
        let mut freeze = QuitFreeze {
            flag: &host.quitting,
            committed: false,
        };
        let result = (|| -> Result<(), String> {
            let deadline = Instant::now() + Duration::from_secs(5);
            let _writes = loop {
                match host.write_gate.try_write() {
                    Ok(guard) => break guard,
                    Err(std::sync::TryLockError::WouldBlock) if Instant::now() < deadline => {
                        thread::sleep(Duration::from_millis(25))
                    }
                    _ => return Err("仍有设置或录音请求在处理，请稍后再安装更新。".into()),
                }
            };
            if host
                .proxy
                .has_active_work()
                .map_err(|_| "无法确认任务状态，请稍后重试。")?
            {
                return Err("仍有生成或模型下载任务，请完成后再安装更新。".into());
            }
            let mut backend = host.backend.lock().map_err(|_| "无法停止本机服务。")?;
            {
                let mut v = manager.view.lock().unwrap();
                v.status = "installing".into();
                v.error = None;
            }
            manager.publish(&app);
            let mut payload = manager.payload.lock().unwrap();
            let download = payload.download.as_ref().ok_or("请先下载并校验更新。")?;
            let mut helper = prepare_helper(download, &manager.target)?;
            if let Err(error) = atomic_json(
                &manager.cache.join("last-request.json"),
                &json!({"directory":download.directory.path()}),
            ) {
                let _ = fs::write(download.directory.path().join("cancel.marker"), b"cancel");
                let _ = helper.kill();
                let _ = helper.wait();
                return Err(error);
            }
            // The helper owns the verified directory after the parent exits.
            let download = payload.download.take().unwrap();
            let _ = download.directory.keep();
            drop(payload);
            backend.stop();
            host.allow_exit.store(true, Ordering::SeqCst);
            freeze.committed = true;
            app.exit(0);
            Ok(())
        })();
        if let Err(error) = result {
            // A failed preparation is retried with a fresh private download.
            if manager.snapshot().status == "installing" {
                manager.payload.lock().unwrap().download = None;
            }
            freeze.flag.store(false, Ordering::SeqCst);
            return Err(manager.error(&app, error));
        }
        Ok(())
    })
    .await
    .map_err(|_| "无法准备安装更新，请重试。".to_string())?
}
