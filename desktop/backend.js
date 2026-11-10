'use strict';
const { spawn } = require('node:child_process');

function startBackend({ python, script, config, dataDirectory, token, env = process.env,
  spawnProcess = spawn, timeout = 30000, onUnexpectedExit = () => {} }) {
  let child;
  let ready = false;
  let stopping = false;
  let exited = false;
  let stopPromise;
  let startTimer;
  let resolveExit;
  const exitPromise = new Promise(resolve => { resolveExit = resolve; });
  const readyPromise = new Promise((resolve, reject) => {
    const rejectStart = message => { clearTimeout(startTimer); reject(new Error(message)); };
    try {
      child = spawnProcess(python, [script, '--desktop', '--port', '0', '--config', config], {
        cwd: dataDirectory, shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...env, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1',
          TTS_LAB_DATA_DIR: dataDirectory, TTS_LAB_DESKTOP_TOKEN: token },
      });
    } catch { rejectStart('无法启动内置语音服务。请完整解压发行包后重试。'); return; }
    let buffer = '';
    child.stdin.on('error', () => {});
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', data => {
      if (ready || stopping) return;
      buffer += data;
      if (buffer.length > 16384) { rejectStart('本机服务返回无效的启动信息。'); child.kill(); return; }
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
        try {
          const info = JSON.parse(line);
          if (info.event === 'ready' && info.host === '127.0.0.1' &&
            Number.isInteger(info.port) && info.port > 0 && info.port <= 65535) {
            ready = true; clearTimeout(startTimer);
            resolve(`http://127.0.0.1:${info.port}`); return;
          }
        } catch { /* Ignore non-protocol lines, never display or save their content. */ }
      }
    });
    // Drain stderr without logging potentially sensitive third-party messages.
    child.stderr.on('data', () => {});
    child.on('error', () => {
      exited = true; resolveExit();
      rejectStart('无法启动内置语音服务。请确认发行包未缺失文件或被安全软件隔离。');
      if (ready && !stopping) onUnexpectedExit();
    });
    child.on('exit', () => {
      exited = true; resolveExit();
      if (!ready) rejectStart('本机服务在启动时退出。请检查配置文件，或重新完整解压发行包。');
      else if (!stopping) onUnexpectedExit();
    });
    startTimer = setTimeout(() => {
      stopping = true; rejectStart('本机服务启动超时。请检查安全软件或重新完整解压发行包。');
      child.kill();
    }, timeout);
  });

  function stop() {
    if (stopPromise) return stopPromise;
    stopping = true; clearTimeout(startTimer);
    stopPromise = (async () => {
      if (!child || exited) return;
      try { child.stdin.end('shutdown\n'); } catch { /* Pipe may already have closed. */ }
      let timer;
      const timeoutPromise = new Promise(resolve => { timer = setTimeout(resolve, 7000); });
      await Promise.race([exitPromise, timeoutPromise]); clearTimeout(timer);
      if (!exited) {
        child.kill();
        let killTimer;
        await Promise.race([exitPromise, new Promise(resolve => { killTimer = setTimeout(resolve, 2000); })]);
        clearTimeout(killTimer);
      }
    })();
    return stopPromise;
  }
  return { ready: readyPromise, stop };
}
module.exports = { startBackend };
