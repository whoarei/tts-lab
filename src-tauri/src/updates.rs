//! Stable Windows updates from this project's fixed GitHub release channel.
//!
//! HTTPS authenticates GitHub and its release CDN; the release's SHA256 sidecar
//! checks downloaded bytes. This is not a detached-signature updater protocol.
use reqwest::{blocking::Client, redirect::Policy, Url};
use semver::Version;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

const REPOSITORY: &str = "https://github.com/whoarei/tts-lab";
const RELEASE_ENDPOINT: &str = "https://api.github.com/repos/whoarei/tts-lab/releases/latest";
const MAX_PACKAGE: u64 = 256 * 1024 * 1024;
const MAX_METADATA: u64 = 1024 * 1024;
const MAX_CHECKSUM: u64 = 512;
const CHECK_ERROR: &str = "无法检查更新，请检查网络连接或稍后重试。";
const RELEASE_ERROR: &str = "更新信息不完整或不符合此应用的发布格式，已停止更新。";
const DOWNLOAD_ERROR: &str = "更新下载失败，请检查网络连接和磁盘空间后重试。现有应用未修改。";
const VERIFY_ERROR: &str = "更新文件校验失败，已丢弃下载内容。现有应用未修改。";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PackageKind {
    Installer,
    Portable,
}

#[derive(Clone, Debug, Serialize)]
pub struct UpdateCandidate {
    pub version: String,
    pub notes: String,
    pub published_at: String,
    pub release_url: String,
    pub download_url: String,
    pub checksum_url: String,
    pub size: u64,
    pub kind: PackageKind,
}

pub struct DownloadedUpdate {
    /// Keep this alive until the native apply helper has taken ownership.
    pub directory: tempfile::TempDir,
    pub path: PathBuf,
    pub sha256: String,
    pub candidate: UpdateCandidate,
}

#[derive(Deserialize)]
struct Release {
    tag_name: String,
    draft: bool,
    prerelease: bool,
    body: Option<String>,
    published_at: Option<String>,
    #[serde(default)]
    assets: Vec<Asset>,
}

#[derive(Deserialize)]
struct Asset {
    name: String,
    size: u64,
    browser_download_url: String,
}

struct Fetch {
    reader: Box<dyn Read>,
    content_length: Option<u64>,
}

trait Transport {
    fn fetch(&self, url: &str, package: bool) -> Result<Fetch, String>;
}

struct HttpsTransport {
    metadata: Client,
    package: Client,
}

impl HttpsTransport {
    fn new() -> Result<Self, String> {
        fn client(timeout: Duration) -> Result<Client, String> {
            Client::builder()
                // Preserve the system/provisioned HTTPS proxy and native CA
                // roots. Provider API keys and GitHub tokens are never sent.
                .https_only(true)
                .user_agent(concat!("TTS-Lab/", env!("CARGO_PKG_VERSION")))
                .connect_timeout(Duration::from_secs(10))
                .timeout(timeout)
                .redirect(Policy::custom(|attempt| {
                    if attempt.previous().len() > 5 || !allowed_https_url(attempt.url()) {
                        attempt.error("Update redirect rejected")
                    } else {
                        attempt.follow()
                    }
                }))
                .build()
                .map_err(|_| CHECK_ERROR.into())
        }
        Ok(Self {
            metadata: client(Duration::from_secs(15))?,
            package: client(Duration::from_secs(10 * 60))?,
        })
    }
}

impl Transport for HttpsTransport {
    fn fetch(&self, url: &str, package: bool) -> Result<Fetch, String> {
        let parsed = Url::parse(url).map_err(|_| CHECK_ERROR)?;
        if !allowed_https_url(&parsed) {
            return Err(CHECK_ERROR.into());
        }
        let client = if package {
            &self.package
        } else {
            &self.metadata
        };
        let response = client
            .get(parsed)
            .header(
                "Accept",
                if url == RELEASE_ENDPOINT {
                    "application/vnd.github+json"
                } else {
                    "application/octet-stream"
                },
            )
            .send()
            .map_err(|_| CHECK_ERROR)?;
        if response.status() != reqwest::StatusCode::OK || !allowed_https_url(response.url()) {
            return Err(CHECK_ERROR.into());
        }
        let content_length = response.content_length();
        Ok(Fetch {
            reader: Box::new(response),
            content_length,
        })
    }
}

fn allowed_https_url(url: &Url) -> bool {
    url.scheme() == "https"
        && url.username().is_empty()
        && url.password().is_none()
        && url.port_or_known_default() == Some(443)
        && url.fragment().is_none()
        && matches!(
            url.host_str(),
            Some(
                "github.com"
                    | "api.github.com"
                    | "release-assets.githubusercontent.com"
                    | "objects.githubusercontent.com"
                    | "github-releases.githubusercontent.com"
            )
        )
}

pub fn check_release(current: &str, kind: PackageKind) -> Result<Option<UpdateCandidate>, String> {
    check_with_transport(current, kind, &HttpsTransport::new()?)
}

fn check_with_transport(
    current: &str,
    kind: PackageKind,
    transport: &impl Transport,
) -> Result<Option<UpdateCandidate>, String> {
    let metadata = bounded_read(transport.fetch(RELEASE_ENDPOINT, false)?, MAX_METADATA)
        .map_err(|_| CHECK_ERROR)?;
    candidate_from_json(current, kind, &metadata)
}

fn candidate_from_json(
    current: &str,
    kind: PackageKind,
    metadata: &[u8],
) -> Result<Option<UpdateCandidate>, String> {
    let current = Version::parse(current).map_err(|_| RELEASE_ERROR)?;
    let release: Release = serde_json::from_slice(metadata).map_err(|_| RELEASE_ERROR)?;
    if release.draft || release.prerelease {
        return Ok(None);
    }
    let version = release.tag_name.strip_prefix('v').ok_or(RELEASE_ERROR)?;
    let parsed = Version::parse(version).map_err(|_| RELEASE_ERROR)?;
    if parsed.to_string() != version {
        return Err(RELEASE_ERROR.into());
    }
    if !parsed.pre.is_empty() {
        return Ok(None);
    }
    if !parsed.cmp_precedence(&current).is_gt() {
        return Ok(None);
    }
    let name = asset_name(version, kind);
    let checksum_name = format!("{name}.sha256");
    let download_url = asset_url(version, &name);
    let checksum_url = asset_url(version, &checksum_name);
    let select = |name: &str| -> Result<&Asset, String> {
        let mut matches = release.assets.iter().filter(|asset| asset.name == name);
        let asset = matches.next().ok_or(RELEASE_ERROR)?;
        if matches.next().is_some() {
            return Err(RELEASE_ERROR.into());
        }
        Ok(asset)
    };
    let asset = select(&name)?;
    let checksum = select(&checksum_name)?;
    if asset.size == 0
        || asset.size > MAX_PACKAGE
        || checksum.size == 0
        || checksum.size > MAX_CHECKSUM
        || asset.browser_download_url != download_url
        || checksum.browser_download_url != checksum_url
    {
        return Err(RELEASE_ERROR.into());
    }
    let published_at = release
        .published_at
        .filter(|date| !date.is_empty() && date.len() <= 64)
        .ok_or(RELEASE_ERROR)?;
    // Notes are displayed as text by the UI, and bounded independently of the
    // entire JSON document so a malformed release cannot fill the app window.
    let notes: String = release
        .body
        .unwrap_or_default()
        .chars()
        .take(16_384)
        .collect();
    Ok(Some(UpdateCandidate {
        version: version.into(),
        notes,
        published_at,
        release_url: format!("{REPOSITORY}/releases/tag/v{version}"),
        download_url,
        checksum_url,
        size: asset.size,
        kind,
    }))
}

fn stable_version(version: &str) -> Result<Version, String> {
    let parsed = Version::parse(version).map_err(|_| RELEASE_ERROR)?;
    if !parsed.pre.is_empty() || parsed.to_string() != version {
        return Err(RELEASE_ERROR.into());
    }
    Ok(parsed)
}

fn asset_name(version: &str, kind: PackageKind) -> String {
    let suffix = match kind {
        PackageKind::Installer => "-setup.exe",
        PackageKind::Portable => ".zip",
    };
    format!("TTS-Lab-{version}-win-x64{suffix}")
}

fn asset_url(version: &str, name: &str) -> String {
    format!("{REPOSITORY}/releases/download/v{version}/{name}")
}

fn validate_candidate(candidate: &UpdateCandidate) -> Result<String, String> {
    stable_version(&candidate.version)?;
    let name = asset_name(&candidate.version, candidate.kind);
    if candidate.size == 0
        || candidate.size > MAX_PACKAGE
        || candidate.release_url != format!("{REPOSITORY}/releases/tag/v{}", candidate.version)
        || candidate.download_url != asset_url(&candidate.version, &name)
        || candidate.checksum_url != asset_url(&candidate.version, &format!("{name}.sha256"))
    {
        return Err(RELEASE_ERROR.into());
    }
    Ok(name)
}

fn bounded_read(mut fetch: Fetch, maximum: u64) -> Result<Vec<u8>, String> {
    if fetch.content_length.is_some_and(|size| size > maximum) {
        return Err(CHECK_ERROR.into());
    }
    let mut bytes = Vec::new();
    fetch
        .reader
        .by_ref()
        .take(maximum + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| CHECK_ERROR)?;
    if bytes.len() as u64 > maximum
        || fetch
            .content_length
            .is_some_and(|size| size != bytes.len() as u64)
    {
        return Err(CHECK_ERROR.into());
    }
    Ok(bytes)
}

fn parse_checksum(contents: &[u8], expected_name: &str) -> Result<String, String> {
    let text = std::str::from_utf8(contents).map_err(|_| VERIFY_ERROR)?;
    let mut lines = text.lines();
    let line = lines.next().ok_or(VERIFY_ERROR)?;
    if lines.next().is_some() || line.len() < 67 {
        return Err(VERIFY_ERROR.into());
    }
    let hash = line.get(..64).ok_or(VERIFY_ERROR)?;
    let suffix = line.get(64..).ok_or(VERIFY_ERROR)?;
    if !hash.bytes().all(|byte| byte.is_ascii_hexdigit())
        || !(suffix == format!("  {expected_name}") || suffix == format!(" *{expected_name}"))
    {
        return Err(VERIFY_ERROR.into());
    }
    Ok(hash.to_ascii_lowercase())
}

pub fn download_update(
    candidate: &UpdateCandidate,
    cache_parent: &Path,
    progress: impl Fn(u64, u64),
) -> Result<DownloadedUpdate, String> {
    download_with_transport(
        candidate,
        cache_parent,
        progress,
        &HttpsTransport::new().map_err(|_| DOWNLOAD_ERROR)?,
    )
}

fn download_with_transport(
    candidate: &UpdateCandidate,
    cache_parent: &Path,
    progress: impl Fn(u64, u64),
    transport: &impl Transport,
) -> Result<DownloadedUpdate, String> {
    let filename = validate_candidate(candidate)?;
    let checksum = bounded_read(
        transport
            .fetch(&candidate.checksum_url, false)
            .map_err(|_| DOWNLOAD_ERROR)?,
        MAX_CHECKSUM,
    )
    .map_err(|_| VERIFY_ERROR)?;
    let expected_hash = parse_checksum(&checksum, &filename)?;
    if !cache_parent.is_absolute() {
        return Err(DOWNLOAD_ERROR.into());
    }
    fs::create_dir_all(cache_parent).map_err(|_| DOWNLOAD_ERROR)?;
    let metadata = fs::symlink_metadata(cache_parent).map_err(|_| DOWNLOAD_ERROR)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(DOWNLOAD_ERROR.into());
    }
    let directory = tempfile::Builder::new()
        .prefix("tts-update-")
        .tempdir_in(cache_parent)
        .map_err(|_| DOWNLOAD_ERROR)?;
    let partial = directory.path().join(format!("{filename}.partial"));
    let ready = directory.path().join(filename);
    let mut fetch = transport
        .fetch(&candidate.download_url, true)
        .map_err(|_| DOWNLOAD_ERROR)?;
    if fetch
        .content_length
        .is_some_and(|length| length != candidate.size)
    {
        return Err(VERIFY_ERROR.into());
    }
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&partial).map_err(|_| DOWNLOAD_ERROR)?;
    let mut hash = Sha256::new();
    let mut written = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    progress(0, candidate.size);
    loop {
        let size = fetch.reader.read(&mut buffer).map_err(|_| DOWNLOAD_ERROR)?;
        if size == 0 {
            break;
        }
        written = written.checked_add(size as u64).ok_or(VERIFY_ERROR)?;
        if written > candidate.size || written > MAX_PACKAGE {
            return Err(VERIFY_ERROR.into());
        }
        file.write_all(&buffer[..size])
            .map_err(|_| DOWNLOAD_ERROR)?;
        hash.update(&buffer[..size]);
        progress(written, candidate.size);
    }
    let actual_hash = format!("{:x}", hash.finalize());
    if written != candidate.size || actual_hash != expected_hash {
        return Err(VERIFY_ERROR.into());
    }
    file.flush().map_err(|_| DOWNLOAD_ERROR)?;
    file.sync_all().map_err(|_| DOWNLOAD_ERROR)?;
    drop(file);
    fs::rename(partial, &ready).map_err(|_| DOWNLOAD_ERROR)?;
    Ok(DownloadedUpdate {
        directory,
        path: ready,
        sha256: actual_hash,
        candidate: candidate.clone(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, collections::HashMap, io::Cursor};

    fn fixture(version: &str, kind: PackageKind, bytes: &[u8]) -> serde_json::Value {
        let name = asset_name(version, kind);
        serde_json::json!({"tag_name":format!("v{version}"),"draft":false,"prerelease":false,
            "published_at":"2026-11-13T00:00:00Z","body":"修复并保留本机数据。",
            "assets":[{"name":name,"size":bytes.len(),"browser_download_url":asset_url(version,&name)},
                {"name":format!("{name}.sha256"),"size":format!("{}  {name}\n", "a".repeat(64)).len(),"browser_download_url":asset_url(version,&format!("{name}.sha256"))}]})
    }

    fn candidate(bytes: &[u8]) -> UpdateCandidate {
        candidate_from_json(
            "0.6.0",
            PackageKind::Installer,
            &serde_json::to_vec(&fixture("0.7.0", PackageKind::Installer, bytes)).unwrap(),
        )
        .unwrap()
        .unwrap()
    }

    struct FakeTransport {
        entries: RefCell<HashMap<String, Fetch>>,
        requested: RefCell<Vec<(String, bool)>>,
    }

    impl FakeTransport {
        fn new(
            candidate: &UpdateCandidate,
            package: &[u8],
            checksum: &[u8],
            length: Option<u64>,
        ) -> Self {
            Self {
                entries: RefCell::new(HashMap::from([
                    (
                        candidate.download_url.clone(),
                        Fetch {
                            reader: Box::new(Cursor::new(package.to_vec())),
                            content_length: length,
                        },
                    ),
                    (
                        candidate.checksum_url.clone(),
                        Fetch {
                            reader: Box::new(Cursor::new(checksum.to_vec())),
                            content_length: Some(checksum.len() as u64),
                        },
                    ),
                ])),
                requested: RefCell::new(Vec::new()),
            }
        }
    }

    impl Transport for FakeTransport {
        fn fetch(&self, url: &str, package: bool) -> Result<Fetch, String> {
            self.requested.borrow_mut().push((url.into(), package));
            self.entries
                .borrow_mut()
                .remove(url)
                .ok_or_else(|| "fixture not found".into())
        }
    }

    fn checksum(candidate: &UpdateCandidate, bytes: &[u8]) -> Vec<u8> {
        format!(
            "{:x}  {}\n",
            Sha256::digest(bytes),
            asset_name(&candidate.version, candidate.kind)
        )
        .into_bytes()
    }

    #[test]
    fn stable_semver_only_updates_newer_versions_and_skips_draft_and_prerelease() {
        let mut json = fixture("0.7.0", PackageKind::Installer, b"data");
        let parse = |current, json: &serde_json::Value| {
            candidate_from_json(
                current,
                PackageKind::Installer,
                &serde_json::to_vec(json).unwrap(),
            )
        };
        assert!(parse("0.6.0", &json).unwrap().is_some());
        assert!(parse("0.7.0", &json).unwrap().is_none());
        assert!(parse("0.8.0", &json).unwrap().is_none());
        assert!(parse("0.7.0+local", &json).unwrap().is_none());
        assert!(parse("0.7.0-beta.1", &json).unwrap().is_some());
        json["draft"] = true.into();
        assert!(parse("0.6.0", &json).unwrap().is_none());
        json["draft"] = false.into();
        json["prerelease"] = true.into();
        assert!(parse("0.6.0", &json).unwrap().is_none());
        json["prerelease"] = false.into();
        json["tag_name"] = "v0.7.0-beta.1".into();
        assert!(parse("0.6.0", &json).unwrap().is_none());
        for invalid in ["0.7.0", "v01.7.0", "v0.7", "v0.7.0/../../x"] {
            json["tag_name"] = invalid.into();
            assert!(parse("0.6.0", &json).is_err());
        }
    }

    #[test]
    fn exact_asset_pair_is_required_for_the_detected_distribution() {
        for kind in [PackageKind::Installer, PackageKind::Portable] {
            let original = fixture("0.7.0", kind, b"data");
            let parse = |json: &serde_json::Value| {
                candidate_from_json("0.6.0", kind, &serde_json::to_vec(json).unwrap())
            };
            assert!(parse(&original).unwrap().is_some());
            for value in [
                "https://evil.example/file.exe",
                "http://github.com/whoarei/tts-lab/file.exe",
                "https://github.com/else/project/file.exe",
            ] {
                let mut json = original.clone();
                json["assets"][0]["browser_download_url"] = value.into();
                assert!(parse(&json).is_err());
            }
            let mut json = original.clone();
            json["assets"][0]["size"] = 0.into();
            assert!(parse(&json).is_err());
            let mut json = original.clone();
            json["assets"][0]["size"] = (MAX_PACKAGE + 1).into();
            assert!(parse(&json).is_err());
            let mut json = original.clone();
            json["assets"][1]["size"] = (MAX_CHECKSUM + 1).into();
            assert!(parse(&json).is_err());
            let mut json = original.clone();
            json["assets"].as_array_mut().unwrap().pop();
            assert!(parse(&json).is_err());
            let mut json = original.clone();
            let duplicate = json["assets"][0].clone();
            json["assets"].as_array_mut().unwrap().push(duplicate);
            assert!(parse(&json).is_err());
        }
    }

    #[test]
    fn redirects_only_allow_https_github_and_exact_release_cdn_hosts() {
        for allowed in [
            "https://github.com/whoarei/tts-lab",
            "https://api.github.com/repos/whoarei/tts-lab/releases/latest",
            "https://release-assets.githubusercontent.com/a?sig=123",
            "https://objects.githubusercontent.com/a",
            "https://github-releases.githubusercontent.com/a",
        ] {
            assert!(allowed_https_url(&Url::parse(allowed).unwrap()));
        }
        for denied in [
            "http://github.com/a",
            "https://github.com.evil/a",
            "https://evil.github.com/a",
            "https://github.com:444/a",
            "https://user@github.com/a",
            "https://user:pass@github.com/a",
            "https://127.0.0.1/a",
            "file:///tmp/file",
            "https://github.com/a#fragment",
        ] {
            assert!(!allowed_https_url(&Url::parse(denied).unwrap()));
        }
    }

    #[test]
    fn checksum_requires_one_complete_line_with_the_exact_asset_filename() {
        let name = "TTS-Lab-0.7.0-win-x64-setup.exe";
        let hash = "ab".repeat(32);
        assert_eq!(
            parse_checksum(format!("{hash}  {name}\n").as_bytes(), name).unwrap(),
            hash
        );
        assert_eq!(
            parse_checksum(
                format!("{} *{name}\r\n", hash.to_uppercase()).as_bytes(),
                name
            )
            .unwrap(),
            hash
        );
        for invalid in [
            hash.clone(),
            format!("{hash}  other.exe\n"),
            format!("{hash}  ../{name}\n"),
            format!("{}  {name}", "g".repeat(64)),
            format!("{hash}  {name}\n{hash}  {name}\n"),
            format!("{hash}  {name}\n\n"),
            format!("{hash} {name}\n"),
        ] {
            assert!(parse_checksum(invalid.as_bytes(), name).is_err());
        }
        assert!(parse_checksum(&[255; 80], name).is_err());
        assert!(parse_checksum(format!("{}😀  {name}", "é".repeat(31)).as_bytes(), name).is_err());
    }

    #[test]
    fn bounded_documents_refuse_oversize_and_truncated_metadata() {
        for (body, length, maximum) in [
            (b"12345".as_slice(), None, 4),
            (b"1".as_slice(), Some(10), 4),
            (b"1".as_slice(), Some(2), 4),
        ] {
            assert!(bounded_read(
                Fetch {
                    reader: Box::new(Cursor::new(body.to_vec())),
                    content_length: length
                },
                maximum
            )
            .is_err());
        }
    }

    #[test]
    fn verified_download_is_private_atomic_and_reports_monotonic_progress() {
        let bytes = vec![0x5a; 150_000];
        let candidate = candidate(&bytes);
        let root = tempfile::tempdir().unwrap();
        let transport = FakeTransport::new(
            &candidate,
            &bytes,
            &checksum(&candidate, &bytes),
            Some(bytes.len() as u64),
        );
        let events = RefCell::new(Vec::new());
        let update = download_with_transport(
            &candidate,
            root.path(),
            |done, total| events.borrow_mut().push((done, total)),
            &transport,
        )
        .unwrap();
        assert_eq!(fs::read(&update.path).unwrap(), bytes);
        assert_eq!(update.sha256, format!("{:x}", Sha256::digest(&bytes)));
        assert_eq!(update.path.parent(), Some(update.directory.path()));
        assert!(!update
            .path
            .with_file_name(format!(
                "{}.partial",
                update.path.file_name().unwrap().to_string_lossy()
            ))
            .exists());
        let progress = events.borrow();
        assert_eq!(progress.first(), Some(&(0, bytes.len() as u64)));
        assert_eq!(
            progress.last(),
            Some(&(bytes.len() as u64, bytes.len() as u64))
        );
        assert!(progress
            .windows(2)
            .all(|w| w[0].0 <= w[1].0 && w[0].1 == w[1].1));
        assert_eq!(
            transport
                .requested
                .borrow()
                .iter()
                .map(|(_, package)| *package)
                .collect::<Vec<_>>(),
            [false, true]
        );
        drop(update);
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }

    #[test]
    fn corruption_truncation_and_length_mismatch_discard_temp_files_and_preserve_old_app() {
        let expected = b"known correct package";
        let candidate = candidate(expected);
        let root = tempfile::tempdir().unwrap();
        let old = root.path().join("TTS Lab.exe");
        fs::write(&old, b"old application").unwrap();
        for (bytes, length) in [
            (b"known corrupt package".as_slice(), None),
            (b"known".as_slice(), None),
            (expected.as_slice(), Some(1)),
            (b"known correct package with extra bytes".as_slice(), None),
        ] {
            let transport =
                FakeTransport::new(&candidate, bytes, &checksum(&candidate, expected), length);
            assert!(
                download_with_transport(&candidate, root.path(), |_, _| {}, &transport).is_err()
            );
            assert_eq!(fs::read(&old).unwrap(), b"old application");
            assert_eq!(fs::read_dir(root.path()).unwrap().count(), 1);
        }
    }

    #[test]
    fn no_binary_request_on_invalid_checksum_candidate_or_unwritable_cache() {
        let bytes = b"package";
        let candidate = candidate(bytes);
        let root = tempfile::tempdir().unwrap();
        let transport = FakeTransport::new(&candidate, bytes, b"malformed checksum", None);
        assert!(download_with_transport(&candidate, root.path(), |_, _| {}, &transport).is_err());
        assert_eq!(transport.requested.borrow().len(), 1);
        let mut invalid = candidate.clone();
        invalid.download_url = "https://evil.example/file.exe".into();
        let transport = FakeTransport::new(&candidate, bytes, &checksum(&candidate, bytes), None);
        assert!(download_with_transport(&invalid, root.path(), |_, _| {}, &transport).is_err());
        assert!(transport.requested.borrow().is_empty());
        let blocked = root.path().join("not-a-directory");
        fs::write(&blocked, b"keep").unwrap();
        let transport = FakeTransport::new(&candidate, bytes, &checksum(&candidate, bytes), None);
        assert!(download_with_transport(&candidate, &blocked, |_, _| {}, &transport).is_err());
        assert_eq!(fs::read(&blocked).unwrap(), b"keep");
        assert_eq!(transport.requested.borrow().len(), 1);
    }
}
