"""Run the local-only application. No provider calls occur at startup."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading

import uvicorn

PROVIDER_ENV = {
    "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY", "MINIMAX_API_KEY",
    "MIMO_API_KEY", "VOLCENGINE_TTS_API_KEY",
    "STEP_API_KEY", "FISH_AUDIO_API_KEY",
    "ELEVENLABS_API_KEY", "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION",
    "GOOGLE_CLOUD_ACCESS_TOKEN", "GOOGLE_CLOUD_PROJECT", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "AWS_REGION",
    "AWS_PROFILE",
}


def configure(desktop: bool, config: Path | None) -> None:
    if desktop:
        token = os.environ.get("TTS_LAB_DESKTOP_TOKEN", "")
        if len(token) < 32 or not token.isascii():
            raise ValueError("Desktop launch requires a secure desktop token.")
        data = os.environ.get("TTS_LAB_DATA_DIR", "")
        if not data or not Path(data).is_absolute():
            raise ValueError("Desktop launch requires an absolute data directory.")
        if config is None or not config.is_absolute() or not config.is_file():
            raise ValueError("Desktop launch requires an existing absolute config file.")
        # Never silently use an inherited API key, local AWS profile, container
        # credentials, or metadata credentials. Only this file configures providers;
        # an explicit AWS_PROFILE opts into that named local AWS profile.
        for key in list(os.environ):
            if key in PROVIDER_ENV or key.startswith("AWS_"):
                os.environ.pop(key, None)
        os.environ.update(AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                          AWS_EC2_METADATA_DISABLED="true", TTS_LAB_DESKTOP="1")
        from dotenv import dotenv_values
        values = dotenv_values(config, encoding="utf-8-sig", interpolate=False)
        for key, value in values.items():
            if key in PROVIDER_ENV and value is not None:
                os.environ[key] = value
        if (values.get("AWS_PROFILE") or "").strip():
            os.environ.pop("AWS_CONFIG_FILE", None)
            os.environ.pop("AWS_SHARED_CREDENTIALS_FILE", None)
    else:
        try:
            from dotenv import load_dotenv
        except ImportError:
            if config is not None:
                raise ValueError("An explicit config requires python-dotenv.") from None
        else:
            load_dotenv(config or Path(__file__).with_name(".env"))


class DesktopServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.should_exit or not self.started:
            return
        address = self.servers[0].sockets[0].getsockname()
        print(json.dumps({"event": "ready", "host": address[0], "port": address[1]}), flush=True)

        def listen_for_parent():
            # EOF also stops an orphan when the desktop parent exits unexpectedly.
            for line in sys.stdin:
                if line.strip() == "shutdown":
                    break
            self.should_exit = True

        threading.Thread(target=listen_for_parent, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(description="TTS Lab 本地语音对比工具")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        configure(args.desktop, args.config)
    except (ValueError, OSError):
        # Do not print parser content, paths, or credentials on configuration errors.
        print("TTS Lab configuration could not be loaded. Check the config file and desktop environment.", file=sys.stderr)
        return 1
    if args.desktop:
        server = DesktopServer(uvicorn.Config(
            "tts_lab.app:app", host="127.0.0.1", port=args.port,
            access_log=False, timeout_graceful_shutdown=5))
        server.run()
        return 0 if server.started else 1
    print(f"\n打开 http://127.0.0.1:{args.port}  开始比较。Ctrl+C 停止。\n")
    uvicorn.run("tts_lab.app:app", host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
