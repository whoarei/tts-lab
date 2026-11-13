//! Out-of-process updater. All validation and the ready acknowledgement happen
//! while the original app is still running; application files change only after
//! its process exits. Portable updates replace only manifest-owned files.
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
#[cfg(not(windows))]
use std::thread;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{self, File, Metadata, OpenOptions},
    io::{self, Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
    process::Command,
    time::{Duration, Instant},
};
use tempfile::TempDir;

pub use crate::updates::PackageKind;

const MAX_PACKAGE: u64 = 256 * 1024 * 1024;
const MAX_EXPANDED: u64 = 1024 * 1024 * 1024;
const MAX_FILES: usize = 15_000;
const MAX_MANIFEST: u64 = 8 * 1024 * 1024;
const MANIFEST: &str = "BUILD-MANIFEST.json";
const APP_METADATA: &str = "resources/app/package.json";
const APP_EXE: &str = "TTS Lab.exe";
const APP_NAME: &str = "tts-lab-desktop";
const REQUIRED: [&str; 4] = [
    APP_EXE,
    APP_METADATA,
    "resources/python/python.exe",
    "resources/backend/run.py",
];
type ApplyResult<T> = Result<T, &'static str>;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApplyRequest {
    pub schema: u32,
    pub kind: PackageKind,
    pub package: PathBuf,
    pub sha256: String,
    pub expected_version: String,
    pub target_dir: PathBuf,
    pub parent_pid: u32,
    pub result_path: PathBuf,
    #[serde(default)]
    pub no_restart: bool,
}

#[derive(Debug, Deserialize)]
struct PayloadManifest {
    version: String,
    target: String,
    desktop: String,
    files: Vec<PayloadFile>,
}

#[derive(Debug, Deserialize)]
struct PayloadFile {
    path: String,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct AppMetadata {
    name: String,
    version: String,
    desktop: String,
}

#[derive(Serialize)]
struct Status<'a> {
    schema: u32,
    status: &'a str,
    version: &'a str,
    message: &'a str,
}

struct ExistingApp {
    root: PathBuf,
    version: semver::Version,
    files: BTreeMap<String, String>,
}

struct PreparedPortable {
    directory: Option<TempDir>,
    source: PathBuf,
    backup: PathBuf,
    files: BTreeMap<String, String>,
    version: String,
}

fn reparse_or_symlink(metadata: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        metadata.file_type().is_symlink() || metadata.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        metadata.file_type().is_symlink()
    }
}

fn absolute_without_parent(path: &Path) -> bool {
    path.is_absolute()
        && !path
            .components()
            .any(|part| matches!(part, Component::ParentDir))
}

fn clean_existing_directory(path: &Path) -> ApplyResult<PathBuf> {
    if !absolute_without_parent(path) {
        return Err("更新路径无效。");
    }
    // Reject junctions as well as symlinks, including in an ancestor. A normal
    // install's drive/root is allowed here; the target itself is checked below.
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part.as_os_str());
        if matches!(part, Component::Prefix(_)) {
            continue;
        }
        let metadata = fs::symlink_metadata(&current).map_err(|_| "更新目录不可用。")?;
        if reparse_or_symlink(&metadata) || !metadata.is_dir() {
            return Err("更新目录不能经过链接或重解析点。");
        }
    }
    let canonical = fs::canonicalize(path).map_err(|_| "无法解析更新目录。")?;
    #[cfg(windows)]
    {
        let text = canonical.to_str().ok_or("更新目录编码无效。")?;
        // fs::canonicalize returns verbatim Windows paths. NSIS's /D= and its
        // existing uninstall registry value use ordinary drive/UNC paths.
        if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{}", rest)));
        }
        if let Some(rest) = text.strip_prefix(r"\\?\") {
            return Ok(PathBuf::from(rest));
        }
    }
    Ok(canonical)
}

fn checked_file(path: &Path, maximum: u64) -> ApplyResult<File> {
    let metadata = fs::symlink_metadata(path).map_err(|_| "更新文件不可用。")?;
    if reparse_or_symlink(&metadata) || !metadata.is_file() || metadata.len() > maximum {
        return Err("更新文件类型或大小无效。");
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // In particular, hold the installer against replacement after SHA256
        // verification and until its process has completed.
        options.share_mode(1); // FILE_SHARE_READ only
    }
    let file = options.open(path).map_err(|_| "无法读取更新文件。")?;
    if !file.metadata().map_err(|_| "无法读取更新文件。")?.is_file() {
        return Err("更新文件类型无效。");
    }
    Ok(file)
}

fn valid_hash(hash: &str) -> bool {
    hash.len() == 64 && hash.bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn hash_reader(reader: &mut impl Read, limit: u64) -> ApplyResult<String> {
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = reader.read(&mut buffer).map_err(|_| "无法读取更新内容。")?;
        if read == 0 {
            break;
        }
        size = size.checked_add(read as u64).ok_or("更新文件过大。")?;
        if size > limit {
            return Err("更新文件过大。");
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn verified_package(request: &ApplyRequest) -> ApplyResult<File> {
    if !valid_hash(&request.sha256) || !absolute_without_parent(&request.package) {
        return Err("更新校验信息无效。");
    }
    let mut file = checked_file(&request.package, MAX_PACKAGE)?;
    let actual = hash_reader(&mut file, MAX_PACKAGE)?;
    if !actual.eq_ignore_ascii_case(&request.sha256) {
        return Err("更新文件 SHA256 校验失败，请重新下载。");
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|_| "无法读取更新文件。")?;
    Ok(file)
}

fn read_json<T: for<'a> Deserialize<'a>>(path: &Path, limit: u64) -> ApplyResult<T> {
    let mut file = checked_file(path, limit)?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "无法读取更新信息。")?;
    if bytes.len() as u64 > limit {
        return Err("更新信息过大。");
    }
    serde_json::from_slice(&bytes).map_err(|_| "更新信息格式无效。")
}

fn atomic_status(path: &Path, status: &str, version: &str, message: &str) -> ApplyResult<()> {
    let parent = path.parent().ok_or("更新状态路径无效。")?;
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if reparse_or_symlink(&metadata) || !metadata.is_file() {
            return Err("更新状态路径无效。");
        }
    }
    let mut temporary =
        tempfile::NamedTempFile::new_in(parent).map_err(|_| "无法保存更新状态。")?;
    serde_json::to_writer(
        &mut temporary,
        &Status {
            schema: 1,
            status,
            version,
            message,
        },
    )
    .map_err(|_| "无法保存更新状态。")?;
    temporary
        .write_all(b"\n")
        .map_err(|_| "无法保存更新状态。")?;
    temporary
        .as_file()
        .sync_all()
        .map_err(|_| "无法保存更新状态。")?;
    temporary.persist(path).map_err(|_| "无法保存更新状态。")?;
    Ok(())
}

fn relative_payload_path(value: &str) -> ApplyResult<PathBuf> {
    if value.is_empty() || value.len() > 2048 || value.starts_with('/') || value.contains('\\') {
        return Err("更新包包含无效路径。");
    }
    for part in value.split('/') {
        if part.is_empty()
            || part == "."
            || part == ".."
            || part.ends_with('.')
            || part.ends_with(' ')
            || part
                .chars()
                .any(|ch| ch.is_control() || "<>:\"|?*".contains(ch))
        {
            return Err("更新包包含无效路径。");
        }
        let stem = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (stem.len() == 4
                && (stem.starts_with("COM") || stem.starts_with("LPT"))
                && matches!(stem.as_bytes()[3], b'1'..=b'9'))
        {
            return Err("更新包包含保留文件名。");
        }
    }
    let parts: Vec<_> = value.split('/').collect();
    let lower: Vec<_> = parts.iter().map(|part| part.to_lowercase()).collect();
    if matches!(lower[0].as_str(), "data" | "models")
        || (lower.len() >= 3
            && lower[0] == "resources"
            && lower[1] == "backend"
            && matches!(lower[2].as_str(), "data" | "models"))
        || lower.iter().any(|part| {
            matches!(
                part.as_str(),
                ".git" | ".venv" | "__pycache__" | "node_modules" | "credentials.env"
            ) || (part.starts_with(".env") && part != ".env.example")
                || part.starts_with(".tts-update-")
        })
    {
        return Err("更新包包含个人数据或缓存路径。");
    }
    Ok(PathBuf::from(value))
}

fn manifest_files(
    manifest: &PayloadManifest,
    expected: &str,
) -> ApplyResult<BTreeMap<String, String>> {
    if manifest.version != expected
        || manifest.desktop != "tauri"
        || manifest.target != "win32-x64"
        || manifest.files.is_empty()
        || manifest.files.len() >= MAX_FILES
    {
        return Err("更新包版本或平台无效。");
    }
    let mut files = BTreeMap::new();
    let mut folded = BTreeSet::new();
    for entry in &manifest.files {
        relative_payload_path(&entry.path)?;
        if entry.path == MANIFEST
            || !valid_hash(&entry.sha256)
            || !folded.insert(entry.path.to_lowercase())
            || files
                .insert(entry.path.clone(), entry.sha256.clone())
                .is_some()
        {
            return Err("更新包文件清单无效。");
        }
    }
    for name in REQUIRED {
        if !files.contains_key(name) {
            return Err("更新包缺少必要的应用文件。");
        }
    }
    Ok(files)
}

fn verify_metadata(path: &Path, version: &str) -> ApplyResult<()> {
    let metadata: AppMetadata = read_json(path, 64 * 1024)?;
    if metadata.name != APP_NAME || metadata.desktop != "tauri" || metadata.version != version {
        return Err("应用身份或版本不匹配。");
    }
    Ok(())
}

fn verify_pe(path: &Path, x64: bool) -> ApplyResult<()> {
    let mut file = checked_file(path, MAX_PACKAGE)?;
    let mut header = [0_u8; 64];
    file.read_exact(&mut header)
        .map_err(|_| "更新程序不是有效的 Windows 应用。")
        .and_then(|_| {
            if &header[..2] != b"MZ" {
                return Err("更新程序不是有效的 Windows 应用。");
            }
            let offset = u32::from_le_bytes(header[0x3c..0x40].try_into().unwrap());
            if offset as u64 > MAX_PACKAGE - 6 {
                return Err("更新程序不是有效的 Windows 应用。");
            }
            file.seek(SeekFrom::Start(offset as u64))
                .map_err(|_| "更新程序不是有效的 Windows 应用。")?;
            let mut signature = [0_u8; 6];
            file.read_exact(&mut signature)
                .map_err(|_| "更新程序不是有效的 Windows 应用。")?;
            if &signature[..4] != b"PE\0\0"
                || (signature[4..] != [0x64, 0x86] && (x64 || signature[4..] != [0x4c, 0x01]))
            {
                return Err("更新程序的 Windows 架构无效。");
            }
            Ok(())
        })
}

fn reject_path_links(root: &Path, relative: &Path) -> ApplyResult<()> {
    let mut current = root.to_path_buf();
    for part in relative.components() {
        current.push(part.as_os_str());
        match fs::symlink_metadata(&current) {
            Ok(metadata) if reparse_or_symlink(&metadata) => {
                return Err("应用目录包含链接或重解析点。")
            }
            Ok(_) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => break,
            Err(_) => return Err("无法检查应用文件路径。"),
        }
    }
    Ok(())
}

fn protected_target(root: &Path, updates_root: &Path) -> bool {
    if root.parent().is_none()
        || root == updates_root
        || root.starts_with(updates_root)
        || updates_root.starts_with(root)
    {
        return true;
    }
    for variable in [
        "USERPROFILE",
        "HOME",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_CONFIG_HOME",
    ] {
        if let Some(value) = std::env::var_os(variable) {
            if let Ok(path) = clean_existing_directory(&PathBuf::from(value)) {
                if root == path {
                    return true;
                }
            }
        }
    }
    // updates_root is <private profile>/updates; none of the private profile
    // (credentials, model weights, audio history) can be an install destination.
    updates_root
        .parent()
        .is_some_and(|profile| root.starts_with(profile))
}

fn validate_existing_target(target: &Path, updates_root: &Path) -> ApplyResult<ExistingApp> {
    let root = clean_existing_directory(target)?;
    if protected_target(&root, updates_root) {
        return Err("不能将个人数据目录作为应用安装目录。");
    }
    reject_path_links(&root, Path::new(MANIFEST))?;
    let manifest: PayloadManifest = read_json(&root.join(MANIFEST), MAX_MANIFEST)?;
    let version = semver::Version::parse(&manifest.version).map_err(|_| "当前应用版本无效。")?;
    let files = manifest_files(&manifest, &manifest.version)?;
    let mut total = 0_u64;
    for name in files.keys() {
        let relative = relative_payload_path(name)?;
        reject_path_links(&root, &relative)?;
        let metadata = fs::symlink_metadata(root.join(relative))
            .map_err(|_| "当前应用文件不完整，请使用完整安装包修复。")?;
        if !metadata.is_file() || metadata.len() > MAX_PACKAGE {
            return Err("当前应用文件不完整。");
        }
        total = total
            .checked_add(metadata.len())
            .ok_or("当前应用文件大小无效。")?;
        if total > MAX_EXPANDED {
            return Err("当前应用文件大小无效。");
        }
    }
    verify_metadata(&root.join(APP_METADATA), &manifest.version)?;
    verify_pe(&root.join(APP_EXE), true)?;
    // Validate the identity-bearing bytes in addition to the manifest shape.
    for name in [APP_EXE, APP_METADATA] {
        let actual = hash_reader(
            &mut checked_file(&root.join(name), MAX_PACKAGE)?,
            MAX_PACKAGE,
        )?;
        if !actual.eq_ignore_ascii_case(&files[name]) {
            return Err("当前应用身份文件校验失败。");
        }
    }
    Ok(ExistingApp {
        root,
        version,
        files,
    })
}

fn cancelled(request_dir: &Path) -> bool {
    request_dir.join("cancel.marker").exists()
}

fn check_cancel(request_dir: &Path) -> ApplyResult<()> {
    if cancelled(request_dir) {
        Err("更新已取消。")
    } else {
        Ok(())
    }
}

fn prepare_portable(
    request: &ApplyRequest,
    old: &ExistingApp,
    request_dir: &Path,
    package: File,
) -> ApplyResult<PreparedPortable> {
    let mut archive = zip::ZipArchive::new(package).map_err(|_| "更新 ZIP 格式无效。")?;
    if archive.len() == 0 || archive.len() > MAX_FILES {
        return Err("更新包文件数量无效。");
    }
    let directory = tempfile::Builder::new()
        .prefix(".tts-update-")
        .tempdir_in(&old.root)
        .map_err(|_| "无法在应用目录暂存更新，请检查权限与磁盘空间。")?;
    let source = directory.path().join("new");
    let backup = directory.path().join("old");
    fs::create_dir(&source).map_err(|_| "无法暂存更新。")?;
    fs::create_dir(&backup).map_err(|_| "无法暂存更新。")?;
    let prefix = format!("TTS-Lab-{}-win-x64/", request.expected_version);
    let mut names = BTreeSet::new();
    let mut folded = BTreeSet::new();
    let mut expanded = 0_u64;
    let mut archive_files = BTreeSet::new();
    for index in 0..archive.len() {
        check_cancel(request_dir)?;
        let member = archive.by_index(index).map_err(|_| "无法读取更新 ZIP。")?;
        let name = std::str::from_utf8(member.name_raw())
            .map_err(|_| "更新 ZIP 文件名编码无效。")?
            .to_string();
        if !name.starts_with(&prefix)
            || !names.insert(name.clone())
            || !folded.insert(name.to_lowercase())
            || member
                .unix_mode()
                .is_some_and(|mode| mode & 0o170000 == 0o120000)
        {
            return Err("更新 ZIP 包含重复、链接或无效根目录。");
        }
        let relative = name.strip_prefix(&prefix).unwrap();
        if relative.is_empty() && member.is_dir() {
            continue;
        }
        let trimmed = relative.strip_suffix('/').unwrap_or(relative);
        let safe = relative_payload_path(trimmed)?;
        if member.is_dir() {
            continue;
        }
        let declared = member.size();
        expanded = expanded
            .checked_add(declared)
            .ok_or("更新 ZIP 展开后过大。")?;
        if declared > MAX_PACKAGE || expanded > MAX_EXPANDED {
            return Err("更新 ZIP 展开后过大。");
        }
        if !archive_files.insert(relative.to_string()) {
            return Err("更新 ZIP 包含重复文件。");
        }
        let output = source.join(safe);
        fs::create_dir_all(output.parent().unwrap()).map_err(|_| "无法暂存更新文件。")?;
        let mut destination = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&output)
            .map_err(|_| "无法暂存更新文件。")?;
        let bytes = io::copy(&mut member.take(declared + 1), &mut destination)
            .map_err(|_| "更新 ZIP 文件校验或写入失败。")?;
        if bytes != declared {
            return Err("更新 ZIP 文件长度无效。");
        }
        destination.sync_all().map_err(|_| "无法保存更新文件。")?;
    }
    let manifest: PayloadManifest = read_json(&source.join(MANIFEST), MAX_MANIFEST)?;
    let files = manifest_files(&manifest, &request.expected_version)?;
    let mut expected: BTreeSet<_> = files.keys().cloned().collect();
    expected.insert(MANIFEST.to_string());
    if expected != archive_files {
        return Err("更新 ZIP 与完整文件清单不一致。");
    }
    for (name, digest) in &files {
        check_cancel(request_dir)?;
        let actual = hash_reader(
            &mut checked_file(&source.join(name), MAX_PACKAGE)?,
            MAX_PACKAGE,
        )?;
        if !actual.eq_ignore_ascii_case(digest) {
            return Err("更新包文件清单 SHA256 校验失败。");
        }
    }
    verify_metadata(&source.join(APP_METADATA), &request.expected_version)?;
    verify_pe(&source.join(APP_EXE), true)?;
    // New paths must not replace something the previous app did not own.
    for name in files
        .keys()
        .map(String::as_str)
        .chain(std::iter::once(MANIFEST))
    {
        let relative = relative_payload_path(name)?;
        reject_path_links(&old.root, &relative)?;
        match fs::symlink_metadata(old.root.join(&relative)) {
            Ok(metadata)
                if !metadata.is_file() || (!old.files.contains_key(name) && name != MANIFEST) =>
            {
                return Err("新版本文件与安装目录中的个人文件冲突，请手动安装。")
            }
            Ok(_) => (),
            Err(error) if error.kind() == io::ErrorKind::NotFound => (),
            Err(_) => return Err("无法检查更新目标文件。"),
        }
    }
    Ok(PreparedPortable {
        directory: Some(directory),
        source,
        backup,
        files,
        version: request.expected_version.clone(),
    })
}

fn create_parent(path: &Path) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

fn rollback(
    old: &ExistingApp,
    plan: &mut PreparedPortable,
    backed_up: &[String],
    installed: &[String],
) -> bool {
    let mut recovered = true;
    for name in installed.iter().rev() {
        let path = old.root.join(name);
        if fs::symlink_metadata(&path).is_ok() && fs::remove_file(&path).is_err() {
            recovered = false;
        }
    }
    // New directories are removed only when empty, never recursively.
    let mut directories = BTreeSet::new();
    for name in installed {
        let mut parent = Path::new(name).parent();
        while let Some(path) = parent.filter(|path| !path.as_os_str().is_empty()) {
            directories.insert(path.to_path_buf());
            parent = path.parent();
        }
    }
    for relative in directories.into_iter().rev() {
        let _ = fs::remove_dir(old.root.join(relative));
    }
    for name in backed_up.iter().rev() {
        let from = plan.backup.join(name);
        let to = old.root.join(name);
        if fs::symlink_metadata(&to).is_ok()
            || create_parent(&to).is_err()
            || fs::rename(from, to).is_err()
        {
            recovered = false;
        }
    }
    if !recovered {
        // Never discard the only remaining old files when the OS prevents a
        // complete rollback. Keep this helper's private recovery directory.
        if let Some(directory) = plan.directory.take() {
            let _ = directory.keep();
        }
    }
    recovered
}

fn apply_portable_with_hook<F>(
    old: &ExistingApp,
    plan: &mut PreparedPortable,
    mut before_install: F,
) -> ApplyResult<()>
where
    F: FnMut(usize, &Path) -> io::Result<()>,
{
    let mut old_names: Vec<_> = old.files.keys().cloned().collect();
    old_names.push(MANIFEST.to_string());
    let mut new_names: Vec<_> = plan.files.keys().cloned().collect();
    // The manifest is the commit marker; install it after all its payload.
    new_names.push(MANIFEST.to_string());
    let mut backed_up = Vec::new();
    let mut installed = Vec::new();
    let result = (|| -> io::Result<()> {
        for name in &old_names {
            let from = old.root.join(name);
            let to = plan.backup.join(name);
            create_parent(&to)?;
            fs::rename(from, to)?;
            backed_up.push(name.clone());
        }
        for (index, name) in new_names.iter().enumerate() {
            let to = old.root.join(name);
            before_install(index, &to)?;
            if fs::symlink_metadata(&to).is_ok() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "destination exists",
                ));
            }
            create_parent(&to)?;
            fs::rename(plan.source.join(name), &to)?;
            installed.push(name.clone());
        }
        verify_metadata(&old.root.join(APP_METADATA), &plan.version)
            .and_then(|_| verify_pe(&old.root.join(APP_EXE), true))
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let written: PayloadManifest = read_json(&old.root.join(MANIFEST), MAX_MANIFEST)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if manifest_files(&written, &plan.version)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?
            != plan.files
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "manifest changed",
            ));
        }
        Ok(())
    })();
    if result.is_err() {
        return if rollback(old, plan, &backed_up, &installed) {
            Err("更新写入失败，已恢复原版本。")
        } else {
            Err("更新写入失败，恢复文件仍保留在应用目录的更新备份中，请手动修复。")
        };
    }
    // Prune only empty directories left by obsolete manifest-owned files.
    let mut old_directories = BTreeSet::new();
    for name in &old_names {
        let mut parent = Path::new(name).parent();
        while let Some(path) = parent.filter(|path| !path.as_os_str().is_empty()) {
            old_directories.insert(path.to_path_buf());
            parent = path.parent();
        }
    }
    for relative in old_directories.into_iter().rev() {
        let _ = fs::remove_dir(old.root.join(relative));
    }
    Ok(())
}

#[cfg(windows)]
fn wait_for_parent(pid: u32, request_dir: &Path) -> ApplyResult<()> {
    use std::ffi::c_void;
    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
        fn CloseHandle(handle: *mut c_void) -> i32;
        fn GetLastError() -> u32;
    }
    if pid == 0 {
        return Err("更新父进程无效。");
    }
    let handle = unsafe { OpenProcess(0x0010_0000, 0, pid) }; // SYNCHRONIZE
    if handle.is_null() {
        return if unsafe { GetLastError() } == 87 {
            check_cancel(request_dir)
        } else {
            Err("无法等待原应用退出。")
        };
    }
    let deadline = Instant::now() + Duration::from_secs(60);
    let result = loop {
        if let Err(error) = check_cancel(request_dir) {
            break Err(error);
        }
        match unsafe { WaitForSingleObject(handle, 100) } {
            0 => break Ok(()),
            258 if Instant::now() < deadline => (),
            258 => break Err("原应用未及时退出，更新已取消。"),
            _ => break Err("无法等待原应用退出。"),
        }
    };
    unsafe {
        CloseHandle(handle);
    }
    result
}

#[cfg(not(windows))]
fn wait_for_parent(pid: u32, request_dir: &Path) -> ApplyResult<()> {
    if pid == 0 {
        return Err("更新父进程无效。");
    }
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        check_cancel(request_dir)?;
        if !Path::new("/proc").is_dir() {
            return Err("此平台不支持 Windows 更新进程。");
        }
        if !Path::new("/proc").join(pid.to_string()).exists() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("原应用未及时退出，更新已取消。");
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(windows)]
fn install_setup(request: &ApplyRequest, old: &ExistingApp) -> ApplyResult<()> {
    use std::os::windows::process::CommandExt;
    let _locked_package = verified_package(request)?;
    let status = Command::new(&request.package)
        .arg("/S")
        // NSIS treats everything after /D= as the directory. It must be the
        // final raw, unquoted argument, including when the path has spaces.
        .raw_arg(format!("/D={}", old.root.display()))
        .current_dir(request.result_path.parent().unwrap())
        .status()
        .map_err(|_| "无法启动安装程序，原应用将重新打开。")?;
    if !status.success() {
        return Err("安装程序未成功完成，请检查磁盘空间和文件占用。");
    }
    Ok(())
}

#[cfg(not(windows))]
fn install_setup(_request: &ApplyRequest, _old: &ExistingApp) -> ApplyResult<()> {
    Err("安装器更新仅支持 Windows。")
}

fn default_updates_root() -> ApplyResult<PathBuf> {
    #[cfg(windows)]
    let base = std::env::var_os("LOCALAPPDATA")
        .or_else(|| std::env::var_os("APPDATA"))
        .map(PathBuf::from);
    #[cfg(not(windows))]
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|path| PathBuf::from(path).join(".config")));
    let base = base.ok_or("无法确定用户更新目录。");
    clean_existing_directory(&base?.join("TTS Lab/updates"))
}

fn restart(old: &ExistingApp, no_restart: bool) {
    if no_restart {
        return;
    }
    if verify_pe(&old.root.join(APP_EXE), true).is_ok() {
        let _ = Command::new(old.root.join(APP_EXE))
            .current_dir(&old.root)
            .spawn();
    }
}

fn run_with_root(request_path: &Path, updates_root: &Path) -> i32 {
    let updates_root = match clean_existing_directory(updates_root) {
        Ok(path) => path,
        Err(_) => return 1,
    };
    let request_dir = match request_path
        .parent()
        .and_then(|parent| clean_existing_directory(parent).ok())
    {
        Some(path) if path != updates_root && path.starts_with(&updates_root) => path,
        _ => return 1,
    };
    if request_path.file_name() != Some(std::ffi::OsStr::new("request.json")) {
        return 1;
    }
    let result_path = request_dir.join("result.json");
    if request_dir.join("ready.json").exists() || result_path.exists() {
        return 1;
    }
    let outcome = (|| -> ApplyResult<()> {
        if !absolute_without_parent(request_path) {
            return Err("更新请求路径无效。");
        }
        let request: ApplyRequest = read_json(request_path, 16 * 1024)?;
        if request.schema != 1
            || request.parent_pid == 0
            || request.result_path.file_name() != Some(std::ffi::OsStr::new("result.json"))
            || !absolute_without_parent(&request.result_path)
            || request
                .result_path
                .parent()
                .and_then(|path| clean_existing_directory(path).ok())
                .as_ref()
                != Some(&request_dir)
        {
            return Err("更新请求格式或状态路径无效。");
        }
        let expected =
            semver::Version::parse(&request.expected_version).map_err(|_| "更新版本无效。");
        let expected = expected?;
        if expected.to_string() != request.expected_version {
            return Err("更新版本格式无效。");
        }
        let old = validate_existing_target(&request.target_dir, &updates_root)?;
        if expected <= old.version {
            return Err("不能安装相同或更早的版本。");
        }
        let package_parent = request.package.parent().ok_or("更新文件路径无效。")?;
        let package_parent = clean_existing_directory(package_parent)?;
        if !absolute_without_parent(&request.package)
            || package_parent.starts_with(&old.root)
            || request_dir.starts_with(&old.root)
        {
            return Err("更新文件必须位于应用目录之外。");
        }
        let package = verified_package(&request)?;
        let mut portable = match request.kind {
            PackageKind::Portable => Some(prepare_portable(&request, &old, &request_dir, package)?),
            PackageKind::Installer => {
                verify_pe(&request.package, false)?;
                drop(package);
                None
            }
        };
        check_cancel(&request_dir)?;
        atomic_status(
            &request_dir.join("ready.json"),
            "ready",
            &request.expected_version,
            "更新已验证，等待原应用退出。",
        )?;
        let mut parent_exited = false;
        let apply_result = (|| -> ApplyResult<()> {
            wait_for_parent(request.parent_pid, &request_dir)?;
            parent_exited = true;
            check_cancel(&request_dir)?;
            // Recheck the complete source package after waiting for the parent.
            let _rechecked_package = verified_package(&request)?;
            match portable.as_mut() {
                Some(plan) => apply_portable_with_hook(&old, plan, |_, _| Ok(()))?,
                None => install_setup(&request, &old)?,
            }
            let installed = validate_existing_target(&old.root, &updates_root)?;
            if installed.version != expected {
                return Err("安装后的应用版本不匹配，请手动修复。");
            }
            atomic_status(
                &result_path,
                "success",
                &request.expected_version,
                "新版本已安装。",
            )?;
            Ok(())
        })();
        if let Err(error) = apply_result {
            let status = if cancelled(&request_dir) {
                "cancelled"
            } else {
                "failed"
            };
            let _ = atomic_status(&result_path, status, &request.expected_version, error);
            // An ACK can be cancelled while the parent is still running. Do not
            // create a second copy of the app in that case.
            if parent_exited && !cancelled(&request_dir) {
                restart(&old, request.no_restart);
            }
            return Err(error);
        }
        restart(&old, request.no_restart);
        Ok(())
    })();
    match outcome {
        Ok(()) => 0,
        Err(error) => {
            if !result_path.exists() {
                let _ = atomic_status(&result_path, "failed", "", error);
            }
            1
        }
    }
}

/// Entry point used before initializing Tauri, WebView2 or the Python backend.
/// The helper intentionally never removes the request directory: the restarted
/// app owns retention and must read the final status first.
pub fn run(request_path: &Path) -> i32 {
    match default_updates_root() {
        Ok(root) => run_with_root(request_path, &root),
        Err(_) => 1,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use zip::write::SimpleFileOptions;

    fn sha(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    fn pe() -> Vec<u8> {
        let mut bytes = vec![0; 256];
        bytes[..2].copy_from_slice(b"MZ");
        bytes[0x3c..0x40].copy_from_slice(&128_u32.to_le_bytes());
        bytes[128..134].copy_from_slice(b"PE\0\0\x64\x86");
        bytes
    }

    fn payload(version: &str, extra: &[(&str, &[u8])]) -> BTreeMap<String, Vec<u8>> {
        let mut files = BTreeMap::from([
            (APP_EXE.to_string(), pe()),
            (
                APP_METADATA.to_string(),
                serde_json::to_vec(&json!({"name": APP_NAME,"desktop":"tauri","version":version}))
                    .unwrap(),
            ),
            (
                "resources/python/python.exe".to_string(),
                b"python fixture".to_vec(),
            ),
            (
                "resources/backend/run.py".to_string(),
                b"# backend fixture".to_vec(),
            ),
        ]);
        for (path, bytes) in extra {
            files.insert(path.to_string(), bytes.to_vec());
        }
        let entries: Vec<_> = files
            .iter()
            .map(|(path, bytes)| json!({"path":path,"sha256":sha(bytes)}))
            .collect();
        files.insert(
            MANIFEST.to_string(),
            serde_json::to_vec(
                &json!({"version":version,"desktop":"tauri","target":"win32-x64","files":entries}),
            )
            .unwrap(),
        );
        files
    }

    fn write_payload(root: &Path, files: &BTreeMap<String, Vec<u8>>) {
        fs::create_dir_all(root).unwrap();
        for (path, bytes) in files {
            let out = root.join(path);
            fs::create_dir_all(out.parent().unwrap()).unwrap();
            fs::write(out, bytes).unwrap();
        }
    }

    fn write_zip(path: &Path, version: &str, files: &BTreeMap<String, Vec<u8>>) {
        let mut writer = zip::ZipWriter::new(File::create(path).unwrap());
        for (name, bytes) in files {
            writer
                .start_file(
                    format!("TTS-Lab-{version}-win-x64/{name}"),
                    SimpleFileOptions::default(),
                )
                .unwrap();
            writer.write_all(bytes).unwrap();
        }
        writer.finish().unwrap();
    }

    struct Fixture {
        _temp: TempDir,
        updates: PathBuf,
        request_dir: PathBuf,
        target: PathBuf,
        package: PathBuf,
        old_files: BTreeMap<String, Vec<u8>>,
    }

    impl Fixture {
        fn new() -> Self {
            let temp = tempfile::tempdir().unwrap();
            let updates = temp.path().join("profile/updates");
            let request_dir = updates.join("tts-update-test");
            fs::create_dir_all(&request_dir).unwrap();
            let target = temp.path().join("custom app 测试 path");
            let old_files = payload(
                "0.6.0",
                &[("obsolete.dll", b"remove only this known payload")],
            );
            write_payload(&target, &old_files);
            let package = temp.path().join("new.zip");
            Self {
                _temp: temp,
                updates,
                request_dir,
                target,
                package,
                old_files,
            }
        }

        fn request(&self, expected: &str) -> ApplyRequest {
            ApplyRequest {
                schema: 1,
                kind: PackageKind::Portable,
                package: self.package.clone(),
                sha256: sha(&fs::read(&self.package).unwrap()),
                expected_version: expected.to_string(),
                target_dir: self.target.clone(),
                parent_pid: u32::MAX,
                result_path: self.request_dir.join("result.json"),
                no_restart: true,
            }
        }

        fn save_request(&self, request: &ApplyRequest) -> PathBuf {
            let path = self.request_dir.join("request.json");
            fs::write(&path, serde_json::to_vec(request).unwrap()).unwrap();
            path
        }

        fn old(&self) -> ExistingApp {
            validate_existing_target(
                &self.target,
                &clean_existing_directory(&self.updates).unwrap(),
            )
            .unwrap()
        }

        fn assert_original_untouched(&self) {
            for (name, bytes) in &self.old_files {
                assert_eq!(&fs::read(self.target.join(name)).unwrap(), bytes, "{name}");
            }
        }
    }

    #[test]
    fn portable_update_preserves_unknown_and_profile_files_and_removes_only_obsolete_payload() {
        let fixture = Fixture::new();
        let files = payload(
            "0.7.0",
            &[
                ("new-legitimate.dll", b"new DLL"),
                ("docs/说明.txt", "合法文档".as_bytes()),
            ],
        );
        write_zip(&fixture.package, "0.7.0", &files);
        fs::write(fixture.target.join("keep-my-notes.txt"), b"user file").unwrap();
        fs::create_dir_all(fixture.updates.parent().unwrap().join("data")).unwrap();
        let private = fixture.updates.parent().unwrap().join("data/history.wav");
        fs::write(&private, b"private audio").unwrap();
        let request = fixture.request("0.7.0");
        assert_eq!(
            run_with_root(&fixture.save_request(&request), &fixture.updates),
            0
        );
        for (name, bytes) in files {
            assert_eq!(fs::read(fixture.target.join(name)).unwrap(), bytes);
        }
        assert!(!fixture.target.join("obsolete.dll").exists());
        assert_eq!(
            fs::read(fixture.target.join("keep-my-notes.txt")).unwrap(),
            b"user file"
        );
        assert_eq!(fs::read(private).unwrap(), b"private audio");
        let ready: serde_json::Value =
            read_json(&fixture.request_dir.join("ready.json"), 4096).unwrap();
        assert_eq!(ready["status"], "ready");
        let result: serde_json::Value = read_json(&request.result_path, 4096).unwrap();
        assert_eq!(result["status"], "success");
        assert_eq!(result["version"], "0.7.0");
        assert!(!fs::read_dir(&fixture.target).unwrap().any(|entry| entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".tts-update-")));
    }

    #[test]
    fn package_sha_failure_is_reported_before_ready_and_leaves_old_app_running() {
        let fixture = Fixture::new();
        write_zip(&fixture.package, "0.7.0", &payload("0.7.0", &[]));
        let mut request = fixture.request("0.7.0");
        request.sha256 = "0".repeat(64);
        assert_eq!(
            run_with_root(&fixture.save_request(&request), &fixture.updates),
            1
        );
        assert!(!fixture.request_dir.join("ready.json").exists());
        let result: serde_json::Value = read_json(&request.result_path, 4096).unwrap();
        assert_eq!(result["status"], "failed");
        fixture.assert_original_untouched();
    }

    #[test]
    fn downgrade_and_same_version_are_rejected_before_ready() {
        for version in ["0.5.0", "0.6.0"] {
            let fixture = Fixture::new();
            write_zip(&fixture.package, version, &payload(version, &[]));
            let request = fixture.request(version);
            assert_eq!(
                run_with_root(&fixture.save_request(&request), &fixture.updates),
                1
            );
            assert!(!fixture.request_dir.join("ready.json").exists());
            fixture.assert_original_untouched();
        }
    }

    #[test]
    fn traversal_symlink_and_private_payload_paths_are_rejected() {
        for bad in [
            "../escape.txt",
            "/absolute",
            "C:/drive",
            "a\\evil",
            "a/../../evil",
            "data/private.wav",
            "models/weights.bin",
            ".env",
            "credentials.env",
            "resources/backend/data/history",
            "CON.txt",
            "resources/python/has-space ",
            "resources/python/file:stream",
        ] {
            let fixture = Fixture::new();
            let mut files = payload("0.7.0", &[]);
            files.insert(bad.to_string(), b"untrusted".to_vec());
            write_zip(&fixture.package, "0.7.0", &files);
            let request = fixture.request("0.7.0");
            assert!(
                prepare_portable(
                    &request,
                    &fixture.old(),
                    &fixture.request_dir,
                    verified_package(&request).unwrap()
                )
                .is_err(),
                "accepted {bad}"
            );
            fixture.assert_original_untouched();
        }
        let fixture = Fixture::new();
        let mut writer = zip::ZipWriter::new(File::create(&fixture.package).unwrap());
        writer
            .add_symlink(
                "TTS-Lab-0.7.0-win-x64/link",
                "../escape",
                SimpleFileOptions::default(),
            )
            .unwrap();
        writer.finish().unwrap();
        let request = fixture.request("0.7.0");
        assert!(prepare_portable(
            &request,
            &fixture.old(),
            &fixture.request_dir,
            verified_package(&request).unwrap()
        )
        .is_err());
        fixture.assert_original_untouched();
    }

    #[test]
    fn missing_manifest_entry_corrupt_payload_and_case_collision_are_rejected() {
        for change in [0, 1, 2] {
            let fixture = Fixture::new();
            let mut files = payload("0.7.0", &[]);
            match change {
                0 => {
                    files.insert("not-in-manifest.txt".to_string(), b"extra".to_vec());
                }
                1 => {
                    files.insert(
                        "resources/backend/run.py".to_string(),
                        b"changed after manifest".to_vec(),
                    );
                }
                _ => {
                    files.insert(
                        "RESOURCES/backend/run.py".to_string(),
                        b"case collision".to_vec(),
                    );
                }
            }
            write_zip(&fixture.package, "0.7.0", &files);
            let request = fixture.request("0.7.0");
            assert!(prepare_portable(
                &request,
                &fixture.old(),
                &fixture.request_dir,
                verified_package(&request).unwrap()
            )
            .is_err());
            fixture.assert_original_untouched();
        }
    }

    #[test]
    fn newly_owned_filename_cannot_overwrite_a_users_unknown_file() {
        let fixture = Fixture::new();
        fs::write(
            fixture.target.join("new-legitimate.dll"),
            b"user placed this here",
        )
        .unwrap();
        write_zip(
            &fixture.package,
            "0.7.0",
            &payload("0.7.0", &[("new-legitimate.dll", b"new DLL")]),
        );
        let request = fixture.request("0.7.0");
        assert_eq!(
            run_with_root(&fixture.save_request(&request), &fixture.updates),
            1
        );
        assert!(!fixture.request_dir.join("ready.json").exists());
        fixture.assert_original_untouched();
        assert_eq!(
            fs::read(fixture.target.join("new-legitimate.dll")).unwrap(),
            b"user placed this here"
        );
    }

    #[test]
    fn io_failure_after_partial_replacement_rolls_back_replaced_and_obsolete_files() {
        let fixture = Fixture::new();
        fs::write(fixture.target.join("keep-my-notes.txt"), b"keep").unwrap();
        write_zip(
            &fixture.package,
            "0.7.0",
            &payload("0.7.0", &[("new-legitimate.dll", b"new DLL")]),
        );
        let request = fixture.request("0.7.0");
        let old = fixture.old();
        let mut plan = prepare_portable(
            &request,
            &old,
            &fixture.request_dir,
            verified_package(&request).unwrap(),
        )
        .unwrap();
        let result = apply_portable_with_hook(&old, &mut plan, |index, _| {
            if index == 2 {
                Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "injected write failure",
                ))
            } else {
                Ok(())
            }
        });
        assert!(result.unwrap_err().contains("已恢复"));
        fixture.assert_original_untouched();
        assert!(!fixture.target.join("new-legitimate.dll").exists());
        assert_eq!(
            fs::read(fixture.target.join("keep-my-notes.txt")).unwrap(),
            b"keep"
        );
    }

    #[test]
    fn cancellation_before_ready_leaves_no_mutation_or_acknowledgement() {
        let fixture = Fixture::new();
        write_zip(&fixture.package, "0.7.0", &payload("0.7.0", &[]));
        fs::write(fixture.request_dir.join("cancel.marker"), b"cancel").unwrap();
        let request = fixture.request("0.7.0");
        assert_eq!(
            run_with_root(&fixture.save_request(&request), &fixture.updates),
            1
        );
        assert!(!fixture.request_dir.join("ready.json").exists());
        fixture.assert_original_untouched();
    }

    #[test]
    fn result_must_stay_inside_private_request_directory() {
        let fixture = Fixture::new();
        write_zip(&fixture.package, "0.7.0", &payload("0.7.0", &[]));
        let mut request = fixture.request("0.7.0");
        let outside = fixture.target.join("do-not-write.json");
        request.result_path = outside.clone();
        assert_eq!(
            run_with_root(&fixture.save_request(&request), &fixture.updates),
            1
        );
        assert!(!outside.exists());
        assert!(!fixture.request_dir.join("ready.json").exists());
        fixture.assert_original_untouched();
    }

    #[test]
    fn target_must_be_complete_and_not_a_private_profile_or_link() {
        let fixture = Fixture::new();
        fs::remove_file(fixture.target.join("resources/backend/run.py")).unwrap();
        assert!(validate_existing_target(&fixture.target, &fixture.updates).is_err());
        assert!(
            validate_existing_target(fixture.updates.parent().unwrap(), &fixture.updates).is_err()
        );
        #[cfg(unix)]
        {
            let second = Fixture::new();
            let link = second._temp.path().join("linked-install");
            std::os::unix::fs::symlink(&second.target, &link).unwrap();
            assert!(validate_existing_target(&link, &second.updates).is_err());
        }
    }
    #[test]
    fn cancellation_after_ready_stops_wait_without_changing_application_files() {
        let fixture = Fixture::new();
        write_zip(&fixture.package, "0.7.0", &payload("0.7.0", &[]));
        let mut request = fixture.request("0.7.0");
        request.parent_pid = std::process::id();
        let request_path = fixture.save_request(&request);
        let updates_root = fixture.updates.clone();
        let worker = std::thread::spawn(move || run_with_root(&request_path, &updates_root));
        let deadline = Instant::now() + Duration::from_secs(5);
        while !fixture.request_dir.join("ready.json").exists() {
            assert!(
                Instant::now() < deadline,
                "helper never acknowledged validation"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        fs::write(fixture.request_dir.join("cancel.marker"), b"cancel").unwrap();
        assert_eq!(worker.join().unwrap(), 1);
        fixture.assert_original_untouched();
        let result: serde_json::Value = read_json(&request.result_path, 4096).unwrap();
        assert_eq!(result["status"], "cancelled");
    }

    #[test]
    fn failure_while_backing_up_old_files_restores_the_files_already_moved() {
        let fixture = Fixture::new();
        write_zip(&fixture.package, "0.7.0", &payload("0.7.0", &[]));
        let request = fixture.request("0.7.0");
        let old = fixture.old();
        let mut plan = prepare_portable(
            &request,
            &old,
            &fixture.request_dir,
            verified_package(&request).unwrap(),
        )
        .unwrap();
        // Force a real filesystem error after root EXE/obsolete.dll were moved.
        fs::write(
            plan.backup.join("resources"),
            b"blocks creation of backup subdirectory",
        )
        .unwrap();
        assert!(apply_portable_with_hook(&old, &mut plan, |_, _| Ok(())).is_err());
        fixture.assert_original_untouched();
    }

    #[test]
    fn oversized_zip_header_is_rejected_without_allocating_its_claimed_size() {
        let fixture = Fixture::new();
        let mut writer = zip::ZipWriter::new(File::create(&fixture.package).unwrap());
        writer
            .start_file(
                "TTS-Lab-0.7.0-win-x64/bomb.bin",
                SimpleFileOptions::default(),
            )
            .unwrap();
        writer.write_all(b"x").unwrap();
        writer.finish().unwrap();
        let mut bytes = fs::read(&fixture.package).unwrap();
        let central = bytes
            .windows(4)
            .position(|part| part == b"PK\x01\x02")
            .unwrap();
        bytes[central + 24..central + 28]
            .copy_from_slice(&((MAX_PACKAGE + 1) as u32).to_le_bytes());
        bytes[22..26].copy_from_slice(&((MAX_PACKAGE + 1) as u32).to_le_bytes());
        fs::write(&fixture.package, bytes).unwrap();
        let request = fixture.request("0.7.0");
        assert!(prepare_portable(
            &request,
            &fixture.old(),
            &fixture.request_dir,
            verified_package(&request).unwrap()
        )
        .is_err());
        fixture.assert_original_untouched();
    }
}
