//! Own the private Python service and keep its parent-lifetime pipe open.
use std::{
    fs,
    io::{Read, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

const START_ERROR: &str = "无法启动内置语音服务。请检查发行包与配置文件。";

pub struct Backend {
    pub origin: String,
    child: Child,
    stdin: Option<ChildStdin>,
}

impl Backend {
    pub fn start(
        python: &Path,
        script: &Path,
        profile: &Path,
        config: &Path,
        token: String,
    ) -> Result<Self, String> {
        Self::start_with_timeout(
            python,
            script,
            profile,
            config,
            token,
            Duration::from_secs(30),
        )
    }

    fn start_with_timeout(
        python: &Path,
        script: &Path,
        profile: &Path,
        config: &Path,
        token: String,
        timeout: Duration,
    ) -> Result<Self, String> {
        if !script.is_absolute()
            || !profile.is_absolute()
            || !config.is_absolute()
            || !config.is_file()
            || token.len() < 32
            || !token.is_ascii()
        {
            return Err(START_ERROR.into());
        }
        let data = profile.join("data");
        let models = profile.join("models");
        fs::create_dir_all(&data).map_err(|_| START_ERROR)?;
        fs::create_dir_all(&models).map_err(|_| START_ERROR)?;
        let mut command = Command::new(python);
        command
            .arg(script)
            .args(["--desktop", "--port", "0", "--config"])
            .arg(config)
            .current_dir(&data)
            .env("PYTHONUTF8", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("TTS_LAB_DATA_DIR", &data)
            .env("TTS_LAB_MODELS_DIR", &models)
            .env("TTS_LAB_DESKTOP_TOKEN", token)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        let mut child = command.spawn().map_err(|_| START_ERROR)?;
        let stdin = child.stdin.take();
        let mut stdout = child.stdout.take().ok_or(START_ERROR)?;
        let mut stderr = child.stderr.take().ok_or(START_ERROR)?;
        // Third-party diagnostics can contain credentials. Drain without logging.
        thread::spawn(move || {
            let _ = std::io::copy(&mut stderr, &mut std::io::sink());
        });
        let (sender, receiver) = mpsc::sync_channel(1);
        thread::spawn(move || {
            let mut buffer = [0u8; 1024];
            let mut pending = Vec::new();
            let mut received = 0usize;
            loop {
                let size = match stdout.read(&mut buffer) {
                    Ok(0) | Err(_) => {
                        let _ = sender.send(Err(()));
                        return;
                    }
                    Ok(size) => size,
                };
                received += size;
                if received > 16_384 {
                    let _ = sender.send(Err(()));
                    return;
                }
                pending.extend_from_slice(&buffer[..size]);
                while let Some(end) = pending.iter().position(|b| *b == b'\n') {
                    let line: Vec<u8> = pending.drain(..=end).collect();
                    if let Some(port) = ready_port(&line) {
                        let _ = sender.send(Ok(port));
                        // Keep stdout open too, even if the service writes later.
                        let _ = std::io::copy(&mut stdout, &mut std::io::sink());
                        return;
                    }
                }
            }
        });
        match receiver.recv_timeout(timeout) {
            Ok(Ok(port)) => Ok(Self {
                origin: format!("http://127.0.0.1:{port}"),
                child,
                stdin,
            }),
            _ => {
                drop(stdin);
                let _ = child.kill();
                let _ = child.wait();
                Err(START_ERROR.into())
            }
        }
    }

    pub fn is_running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    pub fn stop(&mut self) {
        if let Some(mut stdin) = self.stdin.take() {
            let _ = stdin.write_all(b"shutdown\n");
            // EOF also shuts down the child if the explicit line is lost.
        }
        let deadline = Instant::now() + Duration::from_secs(7);
        while self.is_running() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(25));
        }
        if self.is_running() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.stop();
    }
}

fn ready_port(line: &[u8]) -> Option<u16> {
    let value: serde_json::Value = serde_json::from_slice(line).ok()?;
    if value.get("event")?.as_str()? != "ready" || value.get("host")?.as_str()? != "127.0.0.1" {
        return None;
    }
    let port = value.get("port")?.as_u64()?;
    if (1..=u16::MAX as u64).contains(&port) {
        Some(port as u16)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_requires_exact_loopback_and_an_integer_port() {
        assert_eq!(
            ready_port(br#"{"event":"ready","host":"127.0.0.1","port":12345}"#),
            Some(12345)
        );
        for invalid in [
            r#"{"event":"ready","host":"0.0.0.0","port":12345}"#,
            r#"{"event":"ready","host":"localhost","port":12345}"#,
            r#"{"event":"ready","host":"127.0.0.1","port":0}"#,
            r#"{"event":"ready","host":"127.0.0.1","port":65536}"#,
            r#"{"event":"ready","host":"127.0.0.1","port":"12345"}"#,
        ] {
            assert_eq!(ready_port(invalid.as_bytes()), None);
        }
    }

    #[test]
    fn malformed_and_excessive_startup_output_fail_closed_without_stderr() {
        let Ok(python) = std::env::var("TTS_LAB_PYTHON") else {
            return;
        };
        for contents in [
            "import sys,time\nprint('{\"event\":\"ready\",\"host\":\"0.0.0.0\",\"port\":1234}',flush=True)\nprint('private-provider-key',file=sys.stderr,flush=True)\ntime.sleep(30)\n",
            "import sys,time\nprint('x'*20000,flush=True)\nprint('private-provider-key',file=sys.stderr,flush=True)\ntime.sleep(30)\n",
        ] {
            let profile = tempfile::tempdir().unwrap();
            let config = profile.path().join("credentials.env");
            let script = profile.path().join("bad_service.py");
            fs::write(&config, b"").unwrap();
            fs::write(&script, contents).unwrap();
            let start = Instant::now();
            let result = Backend::start_with_timeout(Path::new(&python), &script, profile.path(), &config, "t".repeat(64), Duration::from_millis(500));
            assert!(start.elapsed() < Duration::from_secs(5));
            assert!(result.is_err());
            assert!(!result.err().unwrap().contains("private-provider-key"));
        }
    }

    #[test]
    fn actual_python_lifetime_authentication_and_persistence() {
        let Ok(python) = std::env::var("TTS_LAB_PYTHON") else {
            return;
        };
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let profile = tempfile::tempdir().unwrap();
        let config = profile.path().join("credentials.env");
        fs::write(&config, b"").unwrap();
        let token = "t".repeat(64);
        let mut backend = Backend::start(
            Path::new(&python),
            &root.join("run.py"),
            profile.path(),
            &config,
            token.clone(),
        )
        .unwrap();
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .build()
            .unwrap();
        assert_eq!(
            client
                .get(format!("{}/api/state", backend.origin))
                .send()
                .unwrap()
                .status(),
            403
        );
        let proxy = crate::proxy::Proxy::new(backend.origin.clone(), token).unwrap();
        let state = proxy.forward(
            http::Request::builder()
                .uri("tts-api://localhost/api/state")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(state.status(), 200);
        let state: serde_json::Value = serde_json::from_slice(state.body()).unwrap();
        assert!(!proxy.has_pending_jobs().unwrap());
        assert!(!proxy.has_active_work().unwrap());
        let request = http::Request::builder()
            .uri("tts-api://localhost/api/batches")
            .method("POST")
            .header("content-type", "application/json")
            .header("x-local-token", state["token"].as_str().unwrap())
            .body(
                br#"{"text":"Tauri offline integration","variants":[{"provider":"demo"}]}"#
                    .to_vec(),
            )
            .unwrap();
        let batch = proxy.forward(request);
        assert_eq!(
            batch.status(),
            202,
            "{}",
            String::from_utf8_lossy(batch.body())
        );
        let batch: serde_json::Value = serde_json::from_slice(batch.body()).unwrap();
        let id = batch["runs"][0]["id"].as_str().unwrap();
        let route = format!("/api/runs/{id}/audio");
        for _ in 0..100 {
            let response = proxy.forward(
                http::Request::builder()
                    .uri(format!("tts-api://localhost{route}"))
                    .body(Vec::new())
                    .unwrap(),
            );
            if response.status() == 200 {
                assert!(response.body().starts_with(b"RIFF"));
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let destination = profile.path().join("export.wav");
        proxy.download(&route, &destination).unwrap();
        assert!(fs::read(&destination).unwrap().starts_with(b"RIFF"));
        let range = proxy.forward(
            http::Request::builder()
                .uri(format!("tts-api://localhost{route}"))
                .header("Range", "bytes=0-15")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(range.status(), 206);
        assert_eq!(range.body().len(), 16);
        assert!(range.headers().get("content-range").is_some());
        assert!(!proxy.has_active_work().unwrap());
        backend.stop();
        assert!(!backend.is_running());
        assert!(profile
            .path()
            .join("data")
            .join(id)
            .join("audio.wav")
            .is_file());
        let mut reopened = Backend::start(
            Path::new(&python),
            &root.join("run.py"),
            profile.path(),
            &config,
            "u".repeat(64),
        )
        .unwrap();
        assert!(reopened.is_running());
        let reopened_proxy =
            crate::proxy::Proxy::new(reopened.origin.clone(), "u".repeat(64)).unwrap();
        assert!(!reopened_proxy.has_active_work().unwrap());
        let history = reopened_proxy.forward(
            http::Request::builder()
                .uri("tts-api://localhost/api/runs")
                .body(Vec::new())
                .unwrap(),
        );
        assert_eq!(history.status(), 200);
        let history: serde_json::Value = serde_json::from_slice(history.body()).unwrap();
        assert!(history["runs"]
            .as_array()
            .unwrap()
            .iter()
            .any(|run| run["id"].as_str() == Some(id)));
        reopened.stop();
    }
}
