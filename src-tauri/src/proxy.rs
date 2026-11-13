//! A bounded, private transport from the app WebView to its one Python child.
//! No arbitrary URLs, filesystem paths, provider keys or desktop token enter JS.
use http::{header, HeaderMap, Method, Request, Response, StatusCode};
use reqwest::blocking::{Client, Response as BackendResponse};
use std::{
    io::{Read, Write},
    path::Path,
    time::Duration,
};

const MAX_BODY: usize = 2 * 1024 * 1024;
const MAX_RESPONSE: u64 = 128 * 1024 * 1024;
// A ZIP can contain six long recordings; native exports stream to disk.
const MAX_EXPORT: u64 = 1024 * 1024 * 1024;
const REQUEST_ERROR: &str = "本机服务响应异常，请检查应用是否仍在运行。";
const EXPORT_ERROR: &str = "导出失败，原有文件未修改。请检查本机服务、保存位置或磁盘空间。";

#[derive(Clone)]
pub struct Proxy {
    origin: String,
    token: String,
    client: Client,
}

impl Proxy {
    pub fn new(origin: String, token: String) -> Result<Self, String> {
        let parsed = reqwest::Url::parse(&origin).map_err(|_| REQUEST_ERROR)?;
        if parsed.scheme() != "http"
            || parsed.host_str() != Some("127.0.0.1")
            || parsed.port().is_none()
            || parsed.path() != "/"
            || parsed.query().is_some()
            || parsed.fragment().is_some()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || token.len() < 32
            || !token.is_ascii()
        {
            return Err(REQUEST_ERROR.into());
        }
        let client = Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|_| REQUEST_ERROR)?;
        Ok(Self {
            origin: origin.trim_end_matches('/').into(),
            token,
            client,
        })
    }

    pub fn forward(&self, request: Request<Vec<u8>>) -> Response<Vec<u8>> {
        let cors_origin = request
            .headers()
            .get(header::ORIGIN)
            .and_then(|h| h.to_str().ok());
        if !trusted_authority(request.uri())
            || cors_origin.is_some_and(|origin| !trusted_origin(origin))
        {
            return error_response(StatusCode::FORBIDDEN, None);
        }
        // Malformed non-UTF8 Origin is not the same as a missing Origin.
        if request.headers().contains_key(header::ORIGIN) && cors_origin.is_none() {
            return error_response(StatusCode::FORBIDDEN, None);
        }
        let route = request
            .uri()
            .path_and_query()
            .map(|part| part.as_str())
            .unwrap_or("");
        if !valid_route(route)
            || !matches!(
                *request.method(),
                Method::GET
                    | Method::POST
                    | Method::PUT
                    | Method::DELETE
                    | Method::OPTIONS
                    | Method::HEAD
            )
        {
            return error_response(StatusCode::FORBIDDEN, cors_origin);
        }
        if request.body().len() > MAX_BODY {
            return error_response(StatusCode::PAYLOAD_TOO_LARGE, cors_origin);
        }
        if request.method() == Method::OPTIONS {
            return with_cors(
                Response::builder().status(204).body(Vec::new()).unwrap(),
                cors_origin,
            );
        }
        let result = self
            .request(
                request.method().clone(),
                route,
                request.headers(),
                request.body().clone(),
            )
            .and_then(|mut upstream| {
                if upstream
                    .content_length()
                    .is_some_and(|size| size > MAX_RESPONSE)
                {
                    return Err(REQUEST_ERROR.into());
                }
                let status = upstream.status();
                let headers = upstream.headers().clone();
                let mut bytes = Vec::new();
                (&mut upstream)
                    .take(MAX_RESPONSE + 1)
                    .read_to_end(&mut bytes)
                    .map_err(|_| REQUEST_ERROR)?;
                if bytes.len() as u64 > MAX_RESPONSE {
                    return Err(REQUEST_ERROR.into());
                }
                let mut response = Response::builder().status(status);
                for name in [
                    "content-type",
                    "content-length",
                    "content-range",
                    "accept-ranges",
                    "content-disposition",
                    "etag",
                    "last-modified",
                    "cache-control",
                    "x-content-type-options",
                ] {
                    if let Some(value) = headers.get(name) {
                        response = response.header(name, value);
                    }
                }
                response.body(bytes).map_err(|_| REQUEST_ERROR.into())
            });
        with_cors(
            result.unwrap_or_else(|_| error_response(StatusCode::BAD_GATEWAY, None)),
            cors_origin,
        )
    }

    fn request(
        &self,
        method: Method,
        route: &str,
        headers: &HeaderMap,
        body: Vec<u8>,
    ) -> Result<BackendResponse, String> {
        if !valid_route(route) {
            return Err(REQUEST_ERROR.into());
        }
        let mut request = self
            .client
            .request(method, format!("{}{route}", self.origin))
            .header("X-Desktop-Token", &self.token)
            .header(header::ORIGIN, &self.origin);
        // The URL sets the exact backend Host. Never forward a caller's Host,
        // Authorization, Cookie, fetch metadata or desktop authentication header.
        for name in ["content-type", "x-local-token", "range", "if-range"] {
            if let Some(value) = headers.get(name) {
                request = request.header(name, value);
            }
        }
        request.body(body).send().map_err(|_| REQUEST_ERROR.into())
    }

    pub fn has_active_work(&self) -> Result<bool, String> {
        let response = self
            .client
            .get(format!("{}/api/activity", self.origin))
            .header("X-Desktop-Token", &self.token)
            .header(header::ORIGIN, &self.origin)
            .timeout(Duration::from_millis(2500))
            .send()
            .map_err(|_| REQUEST_ERROR)?;
        if response.status() != StatusCode::OK {
            return Err(REQUEST_ERROR.into());
        }
        let mut bytes = Vec::new();
        response
            .take(4097)
            .read_to_end(&mut bytes)
            .map_err(|_| REQUEST_ERROR)?;
        if bytes.len() > 4096 {
            return Err(REQUEST_ERROR.into());
        }
        let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| REQUEST_ERROR)?;
        value
            .get("busy")
            .and_then(|v| v.as_bool())
            .ok_or_else(|| REQUEST_ERROR.into())
    }

    pub fn has_pending_jobs(&self) -> Result<bool, String> {
        // Closing an unavailable service must not stall the desktop for the
        // longer export timeout. This fixed route cannot be supplied by JS.
        let response = self
            .client
            .get(format!("{}/api/runs", self.origin))
            .header("X-Desktop-Token", &self.token)
            .header(header::ORIGIN, &self.origin)
            .timeout(Duration::from_millis(2500))
            .send()
            .map_err(|_| REQUEST_ERROR)?;
        if response.status() != StatusCode::OK {
            return Err(REQUEST_ERROR.into());
        }
        let mut bytes = Vec::new();
        response
            .take(4 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| REQUEST_ERROR)?;
        if bytes.len() > 4 * 1024 * 1024 {
            return Err(REQUEST_ERROR.into());
        }
        let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| REQUEST_ERROR)?;
        let runs = value
            .get("runs")
            .and_then(|value| value.as_array())
            .ok_or(REQUEST_ERROR)?;
        Ok(runs.iter().any(|run| {
            matches!(
                run.get("status").and_then(|value| value.as_str()),
                Some("queued" | "running")
            )
        }))
    }

    pub fn export_metadata(&self, route: &str) -> Result<String, String> {
        if !valid_export(route) {
            return Err(EXPORT_ERROR.into());
        }
        let response = self
            .request(Method::GET, route, &HeaderMap::new(), Vec::new())
            .map_err(|_| EXPORT_ERROR)?;
        if response.status() != StatusCode::OK
            || response
                .content_length()
                .is_some_and(|size| size > MAX_EXPORT)
        {
            return Err(EXPORT_ERROR.into());
        }
        if let Some(filename) = response
            .headers()
            .get(header::CONTENT_DISPOSITION)
            .and_then(|value| value.to_str().ok())
            .and_then(safe_disposition_filename)
        {
            return Ok(filename);
        }
        if route == "/api/export.csv" {
            return Ok("tts-results.csv".into());
        }
        let parts: Vec<_> = route.split('/').collect();
        if parts[2] == "batches" {
            return Ok(format!("tts-{}.zip", &parts[3][..8]));
        }
        let extension = match response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
        {
            "audio/mpeg" => "mp3",
            "audio/ogg" => "ogg",
            "audio/opus" => "opus",
            "audio/flac" => "flac",
            "audio/aac" => "aac",
            _ => "wav",
        };
        Ok(format!("tts-audio-{}.{}", &parts[3][..8], extension))
    }

    pub fn download(&self, route: &str, destination: &Path) -> Result<(), String> {
        if !valid_export(route) || !destination.is_absolute() {
            return Err(EXPORT_ERROR.into());
        }
        let parent = destination.parent().ok_or(EXPORT_ERROR)?;
        let mut response = self
            .request(Method::GET, route, &HeaderMap::new(), Vec::new())
            .map_err(|_| EXPORT_ERROR)?;
        if response.status() != StatusCode::OK
            || response
                .content_length()
                .is_some_and(|size| size > MAX_EXPORT)
        {
            return Err(EXPORT_ERROR.into());
        }
        let expected = response.content_length();
        let mut temporary = tempfile::NamedTempFile::new_in(parent).map_err(|_| EXPORT_ERROR)?;
        let copied = std::io::copy(&mut (&mut response).take(MAX_EXPORT + 1), &mut temporary)
            .map_err(|_| EXPORT_ERROR)?;
        if copied > MAX_EXPORT || expected.is_some_and(|size| size != copied) {
            return Err(EXPORT_ERROR.into());
        }
        temporary.flush().map_err(|_| EXPORT_ERROR)?;
        temporary.as_file().sync_all().map_err(|_| EXPORT_ERROR)?;
        temporary.persist(destination).map_err(|_| EXPORT_ERROR)?;
        Ok(())
    }
}

fn trusted_authority(uri: &http::Uri) -> bool {
    matches!(
        (uri.scheme_str(), uri.authority().map(|a| a.as_str())),
        (Some("tts-api"), Some("localhost")) | (Some("http"), Some("tts-api.localhost"))
    )
}

fn trusted_origin(origin: &str) -> bool {
    matches!(origin, "tauri://localhost" | "http://tauri.localhost")
}

fn valid_route(route: &str) -> bool {
    let path = route.split('?').next().unwrap_or("");
    path.starts_with("/api/")
        && !route
            .chars()
            .any(|ch| ch.is_control() || ch == '\\' || ch == '#')
        && !path.contains('%')
        && !path.contains("//")
        && !path.split('/').any(|part| part == "." || part == "..")
}

fn valid_export(route: &str) -> bool {
    if route == "/api/export.csv" {
        return true;
    }
    let parts: Vec<_> = route.split('/').collect();
    parts.len() == 5
        && parts[0].is_empty()
        && parts[1] == "api"
        && parts[3].len() == 32
        && parts[3]
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
        && matches!(
            (parts[2], parts[4]),
            ("runs", "audio") | ("batches", "download")
        )
}

fn safe_disposition_filename(disposition: &str) -> Option<String> {
    let filename = disposition
        .split(';')
        .map(str::trim)
        .find_map(|piece| piece.strip_prefix("filename="))?
        .trim_matches('"');
    if filename.is_empty()
        || filename.len() > 128
        || filename.starts_with('.')
        || filename.ends_with(['.', ' '])
        || !filename
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b' '))
    {
        return None;
    }
    Some(filename.into())
}

fn with_cors(mut response: Response<Vec<u8>>, origin: Option<&str>) -> Response<Vec<u8>> {
    let headers = response.headers_mut();
    // Missing Origin occurs for media requests; the response is still only made
    // available to the app's own WebView origin, never a wildcard or reflection.
    let allowed = origin
        .filter(|origin| trusted_origin(origin))
        .unwrap_or(if cfg!(windows) {
            "http://tauri.localhost"
        } else {
            "tauri://localhost"
        });
    headers.insert("access-control-allow-origin", allowed.parse().unwrap());
    headers.insert(
        "access-control-allow-methods",
        "GET, HEAD, POST, PUT, DELETE, OPTIONS".parse().unwrap(),
    );
    headers.insert(
        "access-control-allow-headers",
        "Content-Type, X-Local-Token, Range, If-Range"
            .parse()
            .unwrap(),
    );
    headers.insert(
        "access-control-expose-headers",
        "Content-Type, Content-Length, Content-Range, Accept-Ranges, Content-Disposition"
            .parse()
            .unwrap(),
    );
    headers.insert("vary", "Origin".parse().unwrap());
    headers.insert("cache-control", "no-store".parse().unwrap());
    headers.insert("x-content-type-options", "nosniff".parse().unwrap());
    response
}

fn error_response(status: StatusCode, origin: Option<&str>) -> Response<Vec<u8>> {
    with_cors(
        Response::builder()
            .status(status)
            .header(header::CONTENT_TYPE, "application/json; charset=utf-8")
            .body(br#"{"detail":"Local service request rejected or unavailable."}"#.to_vec())
            .unwrap(),
        origin,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{io::Read, net::TcpListener, thread};

    #[test]
    fn confinement_rejects_urls_traversal_and_untrusted_origins() {
        for invalid in [
            "/",
            "/static/app.js",
            "http://evil/api/state",
            "/api/../private",
            "/api/%2e%2e/private",
            "/api/./state",
            "/api//state",
            "/api/state#x",
            "/api/\\private",
        ] {
            assert!(!valid_route(invalid), "{invalid}");
        }
        assert!(valid_route("/api/references?name=%E5%8F%82%E8%80%83.wav"));
        for invalid in [
            "http://evil/api/state",
            "tts-api://evil/api/state",
            "tts-api://localhost:80/api/state",
            "http://user@tts-api.localhost/api/state",
        ] {
            assert!(!trusted_authority(&invalid.parse().unwrap()));
        }
        assert!(!trusted_origin("http://tauri.localhost.evil"));
        assert!(!trusted_origin("http://tauri.localhost:80"));
        let proxy = Proxy::new("http://127.0.0.1:1".into(), "s".repeat(64)).unwrap();
        let response = proxy.forward(
            Request::builder()
                .uri("tts-api://localhost/api/state")
                .header("Origin", "https://evil.example")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn only_named_exports_are_eligible_for_native_file_access() {
        let id = "a".repeat(32);
        assert!(valid_export("/api/export.csv"));
        assert!(valid_export(&format!("/api/runs/{id}/audio")));
        assert!(valid_export(&format!("/api/batches/{id}/download")));
        for invalid in [
            "/api/state",
            "/api/export.json",
            "/api/export.csv?x=1",
            "/api/runs/../audio",
            "/api/runs/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/audio",
            "https://evil/api/export.csv",
        ] {
            assert!(!valid_export(invalid));
        }
        assert_eq!(
            safe_disposition_filename("attachment; filename=demo-12345678.wav"),
            Some("demo-12345678.wav".into())
        );
        assert!(safe_disposition_filename("attachment; filename=../secret.env").is_none());
    }

    #[test]
    fn preflight_works_without_backend_access_and_upload_body_is_bounded() {
        let proxy = Proxy::new("http://127.0.0.1:1".into(), "x".repeat(64)).unwrap();
        let response = proxy.forward(
            Request::builder()
                .method("OPTIONS")
                .uri("http://tts-api.localhost/api/references")
                .header("Origin", "http://tauri.localhost")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(response.status(), 204);
        assert_eq!(
            response.headers()["access-control-allow-origin"],
            "http://tauri.localhost"
        );
        assert!(response.headers()["access-control-allow-headers"]
            .to_str()
            .unwrap()
            .contains("X-Local-Token"));
        let response = proxy.forward(
            Request::builder()
                .method("POST")
                .uri("tts-api://localhost/api/references")
                .body(vec![0; MAX_BODY + 1])
                .unwrap(),
        );
        assert_eq!(response.status(), 413);
    }

    fn server(response: &'static [u8]) -> (String, std::sync::mpsc::Receiver<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (sender, receiver) = std::sync::mpsc::channel();
        thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let mut request = Vec::new();
            let mut buffer = [0u8; 1024];
            while !request.windows(4).any(|bytes| bytes == b"\r\n\r\n") {
                let size = socket.read(&mut buffer).unwrap();
                if size == 0 {
                    break;
                }
                request.extend_from_slice(&buffer[..size]);
            }
            let _ = sender.send(String::from_utf8_lossy(&request).into_owned());
            socket.write_all(response).unwrap();
        });
        (origin, receiver)
    }

    #[test]
    fn forwards_range_and_private_auth_but_never_caller_authority_or_credentials() {
        let (origin, received) = server(b"HTTP/1.1 206 Partial Content\r\nContent-Length: 4\r\nContent-Type: audio/wav\r\nContent-Range: bytes 0-3/100\r\nConnection: close\r\n\r\nRIFF");
        let proxy = Proxy::new(
            origin.clone(),
            "private-token-123456789012345678901234567890".into(),
        )
        .unwrap();
        let response = proxy.forward(
            Request::builder()
                .uri("http://tts-api.localhost/api/runs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/audio")
                .header("Origin", "http://tauri.localhost")
                .header("Host", "evil.example")
                .header("Authorization", "Bearer caller-secret")
                .header("X-Desktop-Token", "caller-token")
                .header("Cookie", "secret=cookie")
                .header("Range", "bytes=0-3")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(response.status(), 206);
        assert_eq!(response.body(), b"RIFF");
        assert_eq!(response.headers()["content-range"], "bytes 0-3/100");
        assert_eq!(
            response.headers()["access-control-allow-origin"],
            "http://tauri.localhost"
        );
        let request = received
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .to_ascii_lowercase();
        assert!(request.contains("range: bytes=0-3"));
        assert!(request.contains("x-desktop-token: private-token-"));
        assert!(request.contains(&format!("origin: {origin}")));
        for secret in [
            "caller-secret",
            "caller-token",
            "evil.example",
            "secret=cookie",
        ] {
            assert!(!request.contains(secret));
        }
        assert!(!String::from_utf8_lossy(response.body()).contains("private-token"));
    }

    #[test]
    fn redirects_are_not_followed_and_failed_downloads_preserve_existing_files() {
        let (origin, _) = server(b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: http://192.0.2.1/private\r\nConnection: close\r\n\r\n");
        let proxy = Proxy::new(origin, "x".repeat(64)).unwrap();
        let directory = tempfile::tempdir().unwrap();
        let destination = directory.path().join("existing.csv");
        std::fs::write(&destination, b"original").unwrap();
        assert!(proxy.download("/api/export.csv", &destination).is_err());
        assert_eq!(std::fs::read(&destination).unwrap(), b"original");
        assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn successful_download_atomically_replaces_and_truncation_leaves_original() {
        let directory = tempfile::tempdir().unwrap();
        let destination = directory.path().join("existing.csv");
        std::fs::write(&destination, b"original").unwrap();
        let (origin, _) =
            server(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nnew");
        Proxy::new(origin, "x".repeat(64))
            .unwrap()
            .download("/api/export.csv", &destination)
            .unwrap();
        assert_eq!(std::fs::read(&destination).unwrap(), b"new");
        let (origin, _) =
            server(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\npartial");
        assert!(Proxy::new(origin, "x".repeat(64))
            .unwrap()
            .download("/api/export.csv", &destination)
            .is_err());
        assert_eq!(std::fs::read(&destination).unwrap(), b"new");
        assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 1);
    }
}
