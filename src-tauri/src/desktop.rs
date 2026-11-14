//! Fixed desktop actions. Credentials and the backend secret stay native.
use crate::{backend::Backend, proxy::Proxy};
use rand::RngCore;
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    env, fs,
    io::Write,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex, RwLock,
    },
    thread,
    time::Duration,
};
use tauri::{Emitter, Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

pub(crate) struct AppState {
    pub(crate) backend: Mutex<Backend>,
    pub(crate) proxy: Proxy,
    profile: PathBuf,
    pub(crate) quitting: AtomicBool,
    pub(crate) allow_exit: AtomicBool,
    pub(crate) smoke: bool,
    pub(crate) write_gate: RwLock<()>,
    smoke_output: Option<PathBuf>,
    _temporary_profile: Option<tempfile::TempDir>,
}

#[derive(Clone, Serialize)]
struct WindowState {
    maximized: bool,
}

fn bundled_origin(url: &tauri::Url) -> bool {
    // Custom schemes have opaque URL origins, so compare the authority explicitly.
    let scheme = if cfg!(windows) { "http" } else { "tauri" };
    url.scheme() == scheme
        && url.host_str()
            == Some(if cfg!(windows) {
                "tauri.localhost"
            } else {
                "localhost"
            })
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
}

pub(crate) fn check_window(window: &WebviewWindow) -> Result<(), String> {
    if window.label() != "main" || !bundled_origin(&window.url().map_err(|_| "拒绝桌面操作。")?)
    {
        return Err("拒绝桌面操作。".into());
    }
    Ok(())
}

fn request_quit(app: tauri::AppHandle, restart: bool, force: bool) {
    if app
        .state::<AppState>()
        .quitting
        .swap(true, Ordering::SeqCst)
    {
        return;
    }
    thread::spawn(move || {
        let state = app.state::<AppState>();
        let pending = !force && !state.smoke && state.proxy.has_pending_jobs().unwrap_or(false);
        if !force && !state.smoke && (restart || pending) {
            let message = if restart {
                "重启应用会中断正在生成的任务，云端请求仍可能完成并计费。配置和历史会保留。"
            } else {
                "仍有任务在生成。退出会中断本机等待，云服务仍可能完成并计费。再次打开后请先检查历史，程序不会自动重发。"
            };
            let confirmed = app
                .dialog()
                .message(message)
                .title("TTS Lab")
                .kind(MessageDialogKind::Warning)
                .buttons(MessageDialogButtons::OkCancelCustom(
                    if restart {
                        "重启应用"
                    } else {
                        "仍然退出"
                    }
                    .into(),
                    "留在窗口".into(),
                ))
                .blocking_show();
            if !confirmed {
                state.quitting.store(false, Ordering::SeqCst);
                return;
            }
        }
        if let Ok(mut backend) = state.backend.lock() {
            backend.stop();
        }
        state.allow_exit.store(true, Ordering::SeqCst);
        if restart {
            app.restart();
        } else {
            app.exit(0);
        }
    });
}

#[tauri::command]
async fn desktop_action(
    window: WebviewWindow,
    app: tauri::AppHandle,
    action: String,
) -> Result<Value, String> {
    check_window(&window)?;
    match action.as_str() {
        "minimize" => window.minimize().map_err(|_| "无法最小化窗口。")?,
        "toggleMaximize" => {
            if window.is_maximized().map_err(|_| "无法读取窗口状态。")? {
                window.unmaximize().map_err(|_| "无法还原窗口。")?;
            } else {
                window.maximize().map_err(|_| "无法最大化窗口。")?;
            }
        }
        "startDragging" => window.start_dragging().map_err(|_| "无法移动窗口。")?,
        "getWindowState" => {
            return Ok(json!({"maximized": window.is_maximized().unwrap_or(false)}))
        }
        "close" => request_quit(app, false, false),
        "restart" => request_quit(app, true, false),
        "openDataDirectory" => {
            open::that_detached(&app.state::<AppState>().profile)
                .map_err(|_| "无法打开配置目录。")?;
        }
        _ => return Err("拒绝桌面操作。".into()),
    }
    Ok(Value::Null)
}

#[tauri::command]
async fn save_export(
    window: WebviewWindow,
    app: tauri::AppHandle,
    route: String,
) -> Result<bool, String> {
    check_window(&window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let state = app.state::<AppState>();
        let name = state.proxy.export_metadata(&route)?;
        let path = if state.smoke {
            let folder = state.profile.join("exports");
            fs::create_dir_all(&folder).map_err(|_| "无法创建导出目录。")?;
            folder.join(name)
        } else {
            let Some(chosen) = app
                .dialog()
                .file()
                .set_title("保存 TTS Lab 导出文件")
                .set_file_name(&name)
                .blocking_save_file()
            else {
                return Ok(false);
            };
            chosen.into_path().map_err(|_| "无法使用此保存位置。")?
        };
        state.proxy.download(&route, &path)?;
        Ok(true)
    })
    .await
    .map_err(|_| "导出文件失败。".to_string())?
}

#[tauri::command]
async fn open_documentation(
    window: WebviewWindow,
    app: tauri::AppHandle,
    url: String,
) -> Result<(), String> {
    check_window(&window)?;
    let parsed = tauri::Url::parse(&url).map_err(|_| "无效的文档地址。")?;
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
    {
        return Err("仅允许打开 HTTPS 文档。".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        if app.state::<AppState>().smoke {
            return Err("测试模式不打开外部网页。".into());
        }
        if app
            .dialog()
            .message(&url)
            .title("在默认浏览器中打开此文档？")
            .buttons(MessageDialogButtons::OkCancelCustom(
                "用浏览器打开".into(),
                "取消".into(),
            ))
            .blocking_show()
        {
            open::that_detached(url).map_err(|_| "无法打开默认浏览器。")?;
        }
        Ok(())
    })
    .await
    .map_err(|_| "无法打开文档。".to_string())?
}

#[tauri::command]
fn smoke_report(window: WebviewWindow, app: tauri::AppHandle, report: Value) -> Result<(), String> {
    check_window(&window)?;
    let state = app.state::<AppState>();
    if !state.smoke {
        return Err("拒绝测试操作。".into());
    }
    let passed = report.get("status").and_then(Value::as_str) == Some("passed");
    write_smoke_report(&state, &report);
    state.quitting.store(true, Ordering::SeqCst);
    if let Ok(mut backend) = state.backend.lock() {
        backend.stop();
    }
    state.allow_exit.store(true, Ordering::SeqCst);
    app.exit(if passed { 0 } else { 1 });
    Ok(())
}

fn write_smoke_report(state: &AppState, report: &Value) {
    if let Some(path) = &state.smoke_output {
        if let Ok(data) = serde_json::to_vec_pretty(report) {
            let _ = fs::write(path, data);
        }
    }
}

fn profile_directory(app: &tauri::AppHandle) -> Result<PathBuf, Box<dyn std::error::Error>> {
    let base = if cfg!(windows) {
        env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or(app.path().config_dir()?)
    } else {
        app.path().config_dir()?
    };
    Ok(base.join("TTS Lab"))
}

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = env::args_os().collect();
    let smoke = args.iter().any(|v| v == "--smoke-test");
    let smoke_output = args
        .windows(2)
        .find(|v| v[0] == "--smoke-output")
        .map(|v| PathBuf::from(&v[1]));
    if smoke && !smoke_output.as_ref().is_some_and(|p| p.is_absolute()) {
        return Err("测试模式需要绝对路径 --smoke-output。".into());
    }
    let temporary = if smoke {
        Some(
            tempfile::Builder::new()
                .prefix("tts-tauri-smoke-")
                .tempdir()?,
        )
    } else {
        None
    };
    let profile = match &temporary {
        Some(temp) => temp.path().to_path_buf(),
        None => profile_directory(app.handle())?,
    };
    fs::create_dir_all(profile.join("data"))?;
    let config = profile.join("credentials.env");
    if !config.exists() {
        let mut options = fs::OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        options
            .open(&config)?
            .write_all(include_bytes!("../../desktop/credentials.env.example"))?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&profile, fs::Permissions::from_mode(0o700))?;
        fs::set_permissions(&config, fs::Permissions::from_mode(0o600))?;
    }
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let resources = env::current_exe()?
        .parent()
        .ok_or("无效的程序路径。")?
        .join("resources");
    let (python, script) = if cfg!(debug_assertions) {
        let default = source.join(if cfg!(windows) {
            ".venv/Scripts/python.exe"
        } else {
            ".venv/bin/python"
        });
        let python = env::var_os("TTS_LAB_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if default.is_file() {
                    default
                } else {
                    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
                }
            });
        (python, source.join("run.py"))
    } else {
        (
            resources.join("python/python.exe"),
            resources.join("backend/run.py"),
        )
    };
    let mut secret = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut secret);
    let token: String = secret.iter().map(|byte| format!("{byte:02x}")).collect();
    let backend = Backend::start(&python, &script, &profile, &config, token.clone())?;
    let proxy = Proxy::new(backend.origin.clone(), token)?;
    let update_manager = crate::desktop_updates::UpdateManager::new(
        env::current_exe()?
            .parent()
            .ok_or("无效的程序目录。")?
            .to_path_buf(),
        &profile,
    );
    let auto_updates = !smoke && update_manager.auto_check_supported();
    app.manage(update_manager);
    app.manage(AppState {
        backend: Mutex::new(backend),
        proxy,
        profile,
        quitting: AtomicBool::new(false),
        allow_exit: AtomicBool::new(false),
        smoke,
        write_gate: RwLock::new(()),
        smoke_output,
        _temporary_profile: temporary,
    });
    let api_origin = if cfg!(windows) {
        "http://tts-api.localhost"
    } else {
        "tts-api://localhost"
    };
    let init = format!(
        "Object.defineProperty(window,'__TTS_API_ORIGIN__',{{value:{}}});Object.defineProperty(window,'__TTS_UPDATES_AUTO__',{{value:{}}});",
        serde_json::to_string(api_origin)?, auto_updates
    );
    let mut builder = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("TTS Lab")
        .inner_size(1360., 920.)
        .min_inner_size(880., 640.)
        .decorations(false)
        .data_directory(app.state::<AppState>().profile.join("webview"))
        .initialization_script(&init)
        .on_navigation(bundled_origin)
        .on_download(|_, _| false)
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .on_permission_request(|_, _| tauri::webview::PermissionResponse::Deny)
        .devtools(cfg!(debug_assertions));
    if smoke {
        builder = builder.initialization_script(include_str!("../../desktop/smoke.js"));
    }
    let window = builder.build()?;
    let handle = app.handle().clone();
    window.on_window_event(move |event| match event {
        tauri::WindowEvent::CloseRequested { api, .. } => {
            if !handle.state::<AppState>().allow_exit.load(Ordering::SeqCst) {
                api.prevent_close();
                request_quit(handle.clone(), false, false);
            }
        }
        tauri::WindowEvent::Resized(_) => {
            if let Some(window) = handle.get_webview_window("main") {
                let _ = window.emit(
                    "desktop:window-state",
                    WindowState {
                        maximized: window.is_maximized().unwrap_or(false),
                    },
                );
            }
        }
        _ => {}
    });
    let handle = app.handle().clone();
    thread::spawn(move || {
        let mut ticks = 0;
        loop {
            thread::sleep(Duration::from_secs(1));
            let state = handle.state::<AppState>();
            if state.quitting.load(Ordering::SeqCst) {
                continue;
            }
            ticks += 1;
            let running = state
                .backend
                .lock()
                .map(|mut p| p.is_running())
                .unwrap_or(false);
            if !running || (state.smoke && ticks > 120) {
                if state.smoke {
                    write_smoke_report(
                        &state,
                        &json!({"status":"failed","error":"Desktop service stopped or UI smoke timed out."}),
                    );
                } else {
                    handle.dialog().message("本机语音服务意外停止。请重新打开应用后检查历史记录。不会自动重试云端请求。")
                    .kind(MessageDialogKind::Error).blocking_show();
                }
                state.quitting.store(true, Ordering::SeqCst);
                if let Ok(mut process) = state.backend.lock() {
                    process.stop();
                }
                state.allow_exit.store(true, Ordering::SeqCst);
                handle.exit(1);
                return;
            }
        }
    });
    Ok(())
}

pub fn run() {
    let mut builder = tauri::Builder::default().plugin(tauri_plugin_dialog::init());
    // Smoke profiles must not activate or attach to an existing user instance.
    if !env::args_os().any(|arg| arg == "--smoke-test") {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }));
    }
    let builder = builder
        .register_asynchronous_uri_scheme_protocol("tts-api", |context, request, responder| {
            if context.webview_label() != "main" {
                responder.respond(
                    http::Response::builder()
                        .status(403)
                        .body(Vec::<u8>::new())
                        .unwrap(),
                );
                return;
            }
            let handle = context.app_handle().clone();
            tauri::async_runtime::spawn_blocking(move || {
                let state = handle.state::<AppState>();
                let mutation = !matches!(
                    *request.method(),
                    http::Method::GET | http::Method::HEAD | http::Method::OPTIONS
                );
                // A mutation holds this permit through the Python response.
                // Installation takes the exclusive permit before checking jobs.
                let permit = if mutation && !state.quitting.load(Ordering::SeqCst) {
                    Some(state.write_gate.read().unwrap_or_else(|e| e.into_inner()))
                } else {
                    None
                };
                if mutation && (permit.is_none() || state.quitting.load(Ordering::SeqCst)) {
                    responder.respond(
                        http::Response::builder()
                            .status(503)
                            .header("content-type", "application/json")
                            .header(
                                "access-control-allow-origin",
                                if cfg!(windows) {
                                    "http://tauri.localhost"
                                } else {
                                    "tauri://localhost"
                                },
                            )
                            .body(
                                r#"{"detail":"应用正在准备更新或重启，请稍后重试。"}"#
                                    .as_bytes()
                                    .to_vec(),
                            )
                            .unwrap(),
                    );
                } else {
                    responder.respond(state.proxy.forward(request));
                }
                drop(permit);
            });
        })
        .invoke_handler(tauri::generate_handler![
            desktop_action,
            save_export,
            open_documentation,
            smoke_report,
            crate::desktop_updates::get_update_state,
            crate::desktop_updates::check_update,
            crate::desktop_updates::download_update,
            crate::desktop_updates::install_update,
        ])
        .setup(setup);
    match builder.build(tauri::generate_context!()) {
        Ok(app) => app.run(|app, event| {
            if let tauri::RunEvent::ExitRequested { api, .. } = event {
                if !app.state::<AppState>().allow_exit.load(Ordering::SeqCst) {
                    api.prevent_exit();
                    request_quit(app.clone(), false, false);
                }
            }
        }),
        Err(_) => {
            startup_failure();
            std::process::exit(1);
        }
    }
}

fn startup_failure() {
    let message =
        "TTS Lab 启动失败，请检查完整发行包、WebView2 与用户目录权限。不会自动重试云端请求。";
    let args: Vec<_> = env::args_os().collect();
    if args.iter().any(|arg| arg == "--smoke-test") {
        if let Some(pair) = args.windows(2).find(|pair| pair[0] == "--smoke-output") {
            let _ = fs::write(
                PathBuf::from(&pair[1]),
                serde_json::to_vec(&json!({"status":"failed","error":message})).unwrap(),
            );
        }
        return;
    }
    #[cfg(windows)]
    {
        // Works even when WebView2 cannot start and the GUI executable has no console.
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(
                window: *mut std::ffi::c_void,
                text: *const u16,
                title: *const u16,
                flags: u32,
            ) -> i32;
        }
        let text: Vec<u16> = message.encode_utf16().chain(Some(0)).collect();
        let title: Vec<u16> = "TTS Lab".encode_utf16().chain(Some(0)).collect();
        unsafe {
            MessageBoxW(std::ptr::null_mut(), text.as_ptr(), title.as_ptr(), 0x10);
        }
    }
    #[cfg(not(windows))]
    eprintln!("{message}");
}
