"""Headless Fire TV streaming support for FieldStation42.

This module is intentionally small and optional.  When enabled from
``confs/main_config.json`` it:

* starts an Xvfb virtual display,
* points mpv at that hidden display,
* optionally creates a PulseAudio/PipeWire-Pulse null sink for mpv audio,
* starts ffmpeg to convert the hidden display/audio into an HLS stream,
* starts a simple HTTP server for the generated ``live.m3u8``.

The normal FieldStation42 player still controls mpv.  The Fire TV only plays the
HLS URL and sends REST channel commands back to FieldStation42.
"""

from __future__ import annotations

import functools
import http.server
import logging
import os
import shutil
import signal
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class _ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FireTVStreamManager:
    """Supervise the optional headless MPV -> FFmpeg -> HLS pipeline."""

    def __init__(self, config: dict[str, Any] | None):
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", False))
        self.log = logging.getLogger("FireTVStream")
        self.processes: list[subprocess.Popen] = []
        self.httpd: _ReusableThreadingTCPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.pulse_module_id: str | None = None

        self.display = str(self.config.get("display", ":42"))
        self.width = int(self.config.get("width", 1280))
        self.height = int(self.config.get("height", 720))
        self.fps = int(self.config.get("fps", 30))
        self.output_dir = Path(self.config.get("output_dir", "runtime/firetv_hls"))
        self.playlist_name = str(self.config.get("playlist_name", "live.m3u8"))
        self.http_host = str(self.config.get("http_host", "0.0.0.0"))
        self.http_port = int(self.config.get("http_port", 8080))
        self.video_bitrate = str(self.config.get("video_bitrate", "2800k"))
        self.audio_bitrate = str(self.config.get("audio_bitrate", "128k"))
        self.hls_time = str(self.config.get("hls_time", 2))
        self.hls_list_size = str(self.config.get("hls_list_size", 5))
        self.use_xvfb = bool(self.config.get("use_xvfb", True))
        self.setup_audio_sink = bool(self.config.get("setup_audio_sink", True))
        self.audio_sink_name = str(self.config.get("audio_sink_name", "fs42sink"))
        self.audio_input = str(self.config.get("audio_input", f"{self.audio_sink_name}.monitor"))
        self.ffmpeg_extra_args = list(self.config.get("ffmpeg_extra_args", []))

    @property
    def playlist_path(self) -> Path:
        return self.output_dir / self.playlist_name

    def _which_or_warn(self, binary: str) -> bool:
        if shutil.which(binary):
            return True
        self.log.error("Required command not found: %s", binary)
        return False

    def _start_process(self, command: list[str], *, name: str) -> subprocess.Popen:
        self.log.info("Starting %s: %s", name, " ".join(command))
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.processes.append(proc)
        return proc

    def _start_xvfb(self) -> None:
        if not self.use_xvfb:
            return
        if not self._which_or_warn("Xvfb"):
            raise RuntimeError("Xvfb is required for firetv_stream; install it with: sudo apt install xvfb")

        display_num = self.display.lstrip(":")
        lock_file = Path(f"/tmp/.X{display_num}-lock")
        if lock_file.exists():
            self.log.warning("%s exists; assuming display %s may already be running", lock_file, self.display)
            return

        self._start_process(
            ["Xvfb", self.display, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"],
            name="Xvfb",
        )
        time.sleep(0.5)

    def _setup_audio(self) -> None:
        if not self.setup_audio_sink:
            return
        if not self._which_or_warn("pactl"):
            self.log.warning("pactl not found; continuing with default PulseAudio input '%s'", self.audio_input)
            return

        # If the sink already exists, reuse it. This makes restarts less fragile.
        check = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True)
        if self.audio_sink_name in check.stdout:
            self.log.info("Audio sink %s already exists; reusing it", self.audio_sink_name)
        else:
            result = subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={self.audio_sink_name}",
                    f"sink_properties=device.description={self.audio_sink_name}",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self.pulse_module_id = result.stdout.strip()
                self.log.info("Created PulseAudio null sink %s module=%s", self.audio_sink_name, self.pulse_module_id)
            else:
                self.log.warning("Could not create PulseAudio sink: %s", result.stderr.strip())

        # MPV launched later by python-mpv-jsonipc inherits this environment.
        os.environ["PULSE_SINK"] = self.audio_sink_name

    def _start_http_server(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(self.output_dir))
        self.httpd = _ReusableThreadingTCPServer((self.http_host, self.http_port), handler)
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, name="FireTVHLSHTTP", daemon=True)
        self.http_thread.start()
        self.log.info("Serving Fire TV HLS at http://%s:%s/%s", self.http_host, self.http_port, self.playlist_name)

    def _start_ffmpeg(self) -> None:
        if not self._which_or_warn("ffmpeg"):
            raise RuntimeError("ffmpeg is required for firetv_stream; install it with: sudo apt install ffmpeg")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Clear old HLS pieces so the Fire TV does not read stale segments on restart.
        for pattern in ("*.m3u8", "*.ts", "*.m4s", "*.tmp"):
            for old_file in self.output_dir.glob(pattern):
                try:
                    old_file.unlink()
                except OSError:
                    pass

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            str(self.config.get("ffmpeg_loglevel", "warning")),
            "-f",
            "x11grab",
            "-framerate",
            str(self.fps),
            "-video_size",
            f"{self.width}x{self.height}",
            "-i",
            f"{self.display}.0",
            "-f",
            "pulse",
            "-i",
            self.audio_input,
            "-c:v",
            "libx264",
            "-preset",
            str(self.config.get("preset", "veryfast")),
            "-tune",
            "zerolatency",
            "-b:v",
            self.video_bitrate,
            "-maxrate",
            self.video_bitrate,
            "-bufsize",
            str(self.config.get("video_bufsize", "5600k")),
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(self.fps * 2),
            "-c:a",
            "aac",
            "-b:a",
            self.audio_bitrate,
            "-ac",
            "2",
            "-ar",
            "48000",
            "-f",
            "hls",
            "-hls_time",
            self.hls_time,
            "-hls_list_size",
            self.hls_list_size,
            "-hls_flags",
            "delete_segments+program_date_time+independent_segments",
        ]
        command.extend(self.ffmpeg_extra_args)
        command.append(str(self.playlist_path))
        self._start_process(command, name="ffmpeg HLS streamer")

    def start(self) -> None:
        if not self.enabled:
            return
        self.log.info("Fire TV streaming mode enabled")
        self._start_xvfb()
        self._setup_audio()

        # MPV launched by StationPlayer must render to this hidden X display.
        os.environ["DISPLAY"] = self.display
        self.log.info("Set DISPLAY=%s for FieldStation42/mpv", self.display)

        self._start_http_server()
        self._start_ffmpeg()

    def stop(self) -> None:
        if not self.enabled:
            return
        self.log.info("Stopping Fire TV streaming mode")
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

        for proc in reversed(self.processes):
            if proc.poll() is not None:
                continue
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
        self.processes.clear()

        if self.pulse_module_id and shutil.which("pactl"):
            subprocess.run(["pactl", "unload-module", self.pulse_module_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.pulse_module_id = None
