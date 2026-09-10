import os
import re
import json
import time
import signal
import shutil
import logging
import subprocess
import threading
from pathlib import Path


class DirectHLSStreamer:
    """
    Direct HLS backend for FieldStation42.

    Normal video/image outputs:
        /tmp/fs42-hls/master.m3u8
        /tmp/fs42-hls/live.m3u8
        /tmp/fs42-hls/stream_0.m3u8
        /tmp/fs42-hls/stream_1.m3u8
        /tmp/fs42-hls/subtitles.vtt
        /tmp/fs42-hls/subtitles_0.vtt
        /tmp/fs42-hls/status.json

    Webpage outputs:
        /tmp/fs42-hls/master.m3u8
        /tmp/fs42-hls/live.m3u8
        /tmp/fs42-hls/web_00001.ts
        /tmp/fs42-hls/status.json

    Webpage support is intentionally isolated:
        - StationPlayer.show_web() should call play_web(web_config)
        - URL/.html passed through play()/loadfile will also be treated as web capture
        - normal video/image streaming does not go through Brave/Chromium/Xvfb
    """

    def __init__(self, config=None):
        self.log = logging.getLogger("DirectHLSStreamer")
        self.config = config or {}

        self.stream_dir = self.config.get("stream_dir", "/tmp/fs42-hls")
        self.port = int(self.config.get("port", 8080))
        self.width = int(self.config.get("width", 1280))
        self.height = int(self.config.get("height", 720))
        self.video_bitrate = self.config.get("video_bitrate", "2500k")
        self.audio_bitrate = self.config.get("audio_bitrate", "128k")

        self.hls_time = str(self.config.get("hls_time", 2))
        self.hls_list_size = str(self.config.get("hls_list_size", 10))
        self.hls_delete_threshold = str(self.config.get("hls_delete_threshold", 10))
        self.preset = self.config.get("preset", "veryfast")

        # FS42 usually calls play(), then seek(). This delay lets us catch both.
        self.start_delay = float(self.config.get("start_delay", 1.5))

        # Subtitle extraction.
        self.subtitle_window_seconds = float(self.config.get("subtitle_window_seconds", 300))
        self.subtitle_extract_timeout = float(self.config.get("subtitle_extract_timeout", 45))

        # Webpage capture settings. Brave is preferred by _get_browser_binary().
        self.web_display = self.config.get("web_display", ":42")
        self.web_sink_name = self.config.get("web_sink_name", "fs42web")
        self.web_browser_profile_dir = self.config.get("web_browser_profile_dir", "/home/aiml/fs42-brave-web")
        # Backward-compatible alias for older code/config names.
        self.web_chrome_profile_dir = self.config.get("web_chrome_profile_dir", self.web_browser_profile_dir)
        self.web_startup_delay = float(self.config.get("web_startup_delay", 5.0))
        self.web_fps = int(self.config.get("web_fps", 30))
        self.web_capture_audio = bool(self.config.get("web_capture_audio", True))

        self.proc = None
        self.http_proc = None
        self.web_xvfb_proc = None
        self.web_browser_proc = None

        self.current_path = None
        self.started_at = None
        self.seek_offset = 0.0
        self.duration = 0.0

        self.stream_generation = int(time.time() * 1000)
        self.stream_started_at = time.time()
        self.subtitle_generation = 0
        self.subtitles_state = "none"

        self.audio_tracks = []
        self.subtitle_tracks = []

        self.pending_path = None
        self.pending_seek = 0.0
        self.pending_timer = None
        self.subtitle_thread = None

        self.lock = threading.RLock()

        # MPV-ish attributes FS42 may touch.
        self.vf = ""
        self.af = ""
        self.panscan = 0.0
        self.keepaspect = True
        self.loop_playlist = None
        self.pause = False
        self.volume = 100
        self.mute = False

        self._prepare_stream_dir(clear=True)
        self._start_http_server()
        self._write_status_file()

    # -------------------------------------------------------------------------
    # Files / status / HTTP server
    # -------------------------------------------------------------------------

    def _prepare_stream_dir(self, clear=False):
        if clear:
            try:
                shutil.rmtree(self.stream_dir, ignore_errors=True)
            except Exception as e:
                self.log.warning("Could not remove stream dir %s: %s", self.stream_dir, e)

        Path(self.stream_dir).mkdir(parents=True, exist_ok=True)

    def _subtitle_path(self):
        return os.path.join(self.stream_dir, "subtitles.vtt")

    def _raw_subtitle_path(self):
        return os.path.join(self.stream_dir, "subtitles.raw.vtt")

    def _subtitle_track_path(self, track_number):
        return os.path.join(self.stream_dir, f"subtitles_{track_number}.vtt")

    def _raw_subtitle_track_path(self, track_number):
        return os.path.join(self.stream_dir, f"subtitles_{track_number}.raw.vtt")

    def _status_path(self):
        return os.path.join(self.stream_dir, "status.json")

    def _remove_subtitles_file(self):
        try:
            for path in Path(self.stream_dir).glob("subtitles*.vtt"):
                try:
                    path.unlink()
                except Exception:
                    pass

            for path in Path(self.stream_dir).glob("subtitles*.raw.vtt"):
                try:
                    path.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def _write_status_file(self, file_path=None, seek_seconds=None, duration=None):
        with self.lock:
            current_file = file_path if file_path is not None else (self.current_path or "")
            status = {
                "generation": self.stream_generation,
                "subtitle_generation": self.subtitle_generation,
                "updated_at": time.time(),
                "stream_started_at": self.stream_started_at,
                "file": current_file,
                "seek_seconds": float(self.seek_offset if seek_seconds is None else seek_seconds),
                "duration": float(self.duration if duration is None else duration),
                "hls": "master.m3u8",
                "legacy_hls": "live.m3u8",
                "subtitles_state": self.subtitles_state,
                "subtitles": "subtitles.vtt" if os.path.exists(self._subtitle_path()) else None,
                "subtitle_tracks": self.subtitle_tracks,
                "audio_tracks": self.audio_tracks,
                "is_webpage": self._is_webpage(current_file),
            }

        tmp_path = self._status_path() + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(status, f)
            os.replace(tmp_path, self._status_path())
        except Exception as e:
            self.log.warning("Could not write status.json: %s", e)

    def _start_http_server(self):
        if self.http_proc and self.http_proc.poll() is None:
            return

        self.log.info("Starting HLS HTTP server on port %s from %s", self.port, self.stream_dir)
        self.http_proc = subprocess.Popen(
            ["python3", "-m", "http.server", str(self.port), "--bind", "0.0.0.0"],
            cwd=self.stream_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

    def _write_legacy_live_alias(self):
        live_path = os.path.join(self.stream_dir, "live.m3u8")
        try:
            with open(live_path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                f.write("#EXT-X-STREAM-INF:BANDWIDTH=3000000\n")
                f.write("master.m3u8\n")
        except Exception as e:
            self.log.warning("Could not write legacy live.m3u8 alias: %s", e)

    # -------------------------------------------------------------------------
    # MPV-like API
    # -------------------------------------------------------------------------

    @property
    def time_pos(self):
        with self.lock:
            if self.proc and self.proc.poll() is None and self.started_at is not None:
                return self.seek_offset + max(0.0, time.time() - self.started_at)
            return None

    def _cancel_pending_start(self):
        if self.pending_timer:
            try:
                self.pending_timer.cancel()
            except Exception:
                pass
            self.pending_timer = None

    def _schedule_start(self, file_path, seek_seconds=0.0):
        with self.lock:
            self.pending_path = file_path
            self.pending_seek = float(seek_seconds or 0.0)
            self._cancel_pending_start()
            self.pending_timer = threading.Timer(self.start_delay, self._start_pending_now)
            self.pending_timer.daemon = True
            self.pending_timer.start()
            self.log.info(
                "Scheduled FFmpeg stream start: input=%s seek=%.2f delay=%.2fs",
                self.pending_path,
                self.pending_seek,
                self.start_delay,
            )

    def _start_pending_now(self):
        with self.lock:
            file_path = self.pending_path
            seek_seconds = self.pending_seek
            self.pending_path = None
            self.pending_seek = 0.0
            self.pending_timer = None

        if file_path:
            self._start_stream(file_path, seek_seconds)

    def play(self, file_path):
        duration = self._get_duration(file_path)
        with self.lock:
            self.current_path = file_path
            self.duration = duration
            self.seek_offset = 0.0
        self._schedule_start(file_path, 0.0)

    def command(self, *args):
        if not args:
            return

        cmd = args[0]

        if cmd == "playlist-clear":
            return

        if cmd == "loadfile":
            if len(args) >= 2:
                file_path = args[1]
                duration = self._get_duration(file_path)
                with self.lock:
                    self.current_path = file_path
                    self.duration = duration
                    self.seek_offset = 0.0
                self._schedule_start(file_path, 0.0)
            return

        if cmd == "seek":
            if len(args) >= 2:
                try:
                    seconds = float(args[1])
                except Exception:
                    seconds = 0.0

                with self.lock:
                    if self.pending_path:
                        self.log.info("Updating pending stream seek to %.2f", seconds)
                        self.pending_seek = seconds
                        self.seek_offset = seconds
                        return

                    current = self.current_path

                    # Webpages are live captures, not seekable.
                    if current and self._is_webpage(current):
                        self.log.info("Ignoring seek %.2f for webpage channel", seconds)
                        return

                    proc_running = self.proc is not None and self.proc.poll() is None
                    just_started = self.started_at is not None and (time.time() - self.started_at) < 2.0

                    if proc_running and just_started and current and seconds < 2.0:
                        self.log.info("Ignoring tiny late startup seek %.2f", seconds)
                        self.seek_offset = seconds
                        return

                    self.seek_offset = seconds

                if current:
                    self._schedule_start(current, seconds)
            return

        if cmd == "show-text":
            return

        if cmd == "af":
            return

        if cmd == "set_property":
            return

        if cmd == "cycle":
            return

        self.log.debug("Ignoring unsupported MPV command: %s", args)

    # -------------------------------------------------------------------------
    # Process cleanup
    # -------------------------------------------------------------------------

    def _kill_process_group(self, proc, name):
        if proc and proc.poll() is None:
            self.log.info("Stopping %s", name)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass

    def _stop_web_processes(self):
        self._kill_process_group(self.web_browser_proc, "web browser")
        self._kill_process_group(self.web_xvfb_proc, "Xvfb")
        self.web_browser_proc = None
        self.web_xvfb_proc = None

    # Backward-compatible name used by older patch attempts.
    def _stop_webpage_processes(self):
        self._stop_web_processes()

    def _stop_ffmpeg(self):
        with self.lock:
            self._cancel_pending_start()
            proc = self.proc
            self.proc = None
            self.started_at = None
            self.seek_offset = 0.0

        self._kill_process_group(proc, "FFmpeg")
        self._stop_web_processes()

    def stop(self):
        self._stop_ffmpeg()

    def stop_web(self):
        self.log.info("Stopping web HLS capture")
        self.stop()

    def terminate(self):
        self._stop_ffmpeg()
        self._stop_web_processes()

        if self.http_proc and self.http_proc.poll() is None:
            self.log.info("Stopping HLS HTTP server")
            try:
                os.killpg(os.getpgid(self.http_proc.pid), signal.SIGTERM)
                self.http_proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(self.http_proc.pid), signal.SIGKILL)
                except Exception:
                    pass

        self.http_proc = None

    # -------------------------------------------------------------------------
    # Webpage support
    # -------------------------------------------------------------------------

    def _is_webpage(self, path):
        value = str(path or "").strip()
        lower = value.lower()
        return (
            lower.startswith("http://")
            or lower.startswith("https://")
            or lower.startswith("file://")
            or lower.endswith(".html")
            or lower.endswith(".htm")
        )

    def _web_url_for_path(self, path):
        value = str(path or "").strip()
        if value.startswith("http://") or value.startswith("https://") or value.startswith("file://"):
            return value
        if value.lower().endswith(".html") or value.lower().endswith(".htm"):
            return Path(value).resolve().as_uri()
        return value

    def _get_browser_binary(self):
        for name in [
            "brave-browser",
            "brave",
            "chromium-browser",
            "chromium",
            "google-chrome",
            "google-chrome-stable",
        ]:
            found = shutil.which(name)
            if found:
                return found
        return None

    # Backward-compatible name used by older patch attempts.
    def _get_chromium_binary(self):
        return self._get_browser_binary()

    def _ensure_web_audio_sink(self):
        if not self.web_capture_audio:
            return False

        pactl = shutil.which("pactl")
        if not pactl:
            self.log.warning("pactl not found; webpage stream will use silent audio")
            return False

        try:
            result = subprocess.run(
                [pactl, "list", "short", "sinks"],
                text=True,
                capture_output=True,
                timeout=5,
            )

            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == self.web_sink_name:
                        return True

            create = subprocess.run(
                [
                    pactl,
                    "load-module",
                    "module-null-sink",
                    f"sink_name={self.web_sink_name}",
                    "sink_properties=device.description=FS42Web",
                ],
                text=True,
                capture_output=True,
                timeout=5,
            )

            if create.returncode != 0:
                self.log.warning("Could not create PulseAudio sink; webpage stream will use silent audio")
                if create.stderr:
                    self.log.warning("pactl: %s", create.stderr.strip())
                return False

            return True

        except Exception as e:
            self.log.warning("PulseAudio sink setup failed: %s", e)
            return False

    # Backward-compatible name used by older patch attempts.
    def _ensure_pulse_sink(self):
        return self._ensure_web_audio_sink()

    def _start_hidden_browser(self, web_url):
        browser = self._get_browser_binary()
        if not browser:
            raise RuntimeError("Browser not found. Install brave-browser, chromium-browser, chromium, or google-chrome.")

        self._stop_web_processes()

        # Do not delete the profile; this preserves Brave/Chromium extensions.
        Path(self.web_chrome_profile_dir).mkdir(parents=True, exist_ok=True)

        display_screen = f"{self.width}x{self.height}x24"
        xvfb_cmd = [
            "Xvfb",
            self.web_display,
            "-screen",
            "0",
            display_screen,
            "-nolisten",
            "tcp",
        ]

        self.log.info("Starting hidden Xvfb: %s", " ".join(xvfb_cmd))
        self.web_xvfb_proc = subprocess.Popen(
            xvfb_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

        time.sleep(1.0)
        if self.web_xvfb_proc.poll() is not None:
            raise RuntimeError("Xvfb failed to start")

        audio_ok = self._ensure_web_audio_sink()

        env = os.environ.copy()
        env["DISPLAY"] = self.web_display
        env.pop("WAYLAND_DISPLAY", None)
        if audio_ok:
            env["PULSE_SINK"] = self.web_sink_name

        browser_cmd = [
            browser,
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-features=AudioServiceSandbox",
            "--autoplay-policy=no-user-gesture-required",
            "--ozone-platform=x11",
            f"--window-size={self.width},{self.height}",
            "--start-fullscreen",
            "--kiosk",
            web_url,
            f"--user-data-dir={self.web_chrome_profile_dir}",
        ]

        self.log.info("Starting hidden browser: %s", web_url)
        self.log.info("Browser binary=%s", browser)
        self.log.info("Browser DISPLAY=%s WAYLAND_DISPLAY=%s", env.get("DISPLAY"), env.get("WAYLAND_DISPLAY"))

        self.web_browser_proc = subprocess.Popen(
            browser_cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )

        time.sleep(self.web_startup_delay)
        if self.web_browser_proc.poll() is not None:
            raise RuntimeError("Browser failed to start")

        return audio_ok

    def _build_web_capture_command(self, audio_ok, out_path, segment_pattern):
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-y",
            "-f",
            "x11grab",
            "-draw_mouse",
            "0",
            "-video_size",
            f"{self.width}x{self.height}",
            "-framerate",
            str(self.web_fps),
            "-i",
            f"{self.web_display}.0",
        ]

        if audio_ok:
            ffmpeg_cmd += [
                "-f",
                "pulse",
                "-i",
                f"{self.web_sink_name}.monitor",
            ]
        else:
            ffmpeg_cmd += [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]

        ffmpeg_cmd += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-vf",
            "format=yuv420p",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            self.preset,
            "-profile:v",
            "main",
            "-level:v",
            "3.1",
            "-tune",
            "zerolatency",
            "-b:v",
            self.video_bitrate,
            "-maxrate",
            self.video_bitrate,
            "-bufsize",
            "5000k",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-max_muxing_queue_size",
            "4096",
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
            "-hls_delete_threshold",
            self.hls_delete_threshold,
            "-hls_flags",
            "delete_segments+omit_endlist+independent_segments+program_date_time",
            "-hls_allow_cache",
            "0",
            "-start_number",
            str(int(time.time())),
            "-reset_timestamps",
            "1",
            "-hls_segment_filename",
            segment_pattern,
            out_path,
        ]

        return ffmpeg_cmd

    def _start_web_hls_capture(self, web_url):
        self.log.info("Starting hidden webpage HLS capture: %s", web_url)

        # Stop normal FFmpeg/file stream first.
        self.stop()
        self._stop_web_processes()

        # Clean HLS directory, but preserve Brave/Chromium profile/extensions.
        self._prepare_stream_dir(clear=True)
        self._start_http_server()

        audio_ok = self._start_hidden_browser(web_url)

        out_path = os.path.join(self.stream_dir, "master.m3u8")
        segment_pattern = os.path.join(self.stream_dir, "web_%05d.ts")
        ffmpeg_cmd = self._build_web_capture_command(audio_ok, out_path, segment_pattern)

        self.log.info("Starting FFmpeg webpage HLS capture")
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

        with self.lock:
            self.proc = proc
            self.started_at = time.time()
            self.current_path = web_url
            self.seek_offset = 0.0
            self.duration = 0.0
            self.stream_generation = int(time.time() * 1000)
            self.stream_started_at = time.time()
            self.subtitle_generation = int(time.time() * 1000)
            self.subtitles_state = "none"
            self.subtitle_tracks = []
            self.audio_tracks = [
                {
                    "track": 0,
                    "stream_index": None,
                    "language": "und",
                    "title": "Webpage Audio" if audio_ok else "Generated Silent Audio",
                    "codec": "pulse" if audio_ok else "generated",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "display_name": "Webpage Audio" if audio_ok else "Generated Silent Audio",
                    "hls_name": "Webpage_Audio" if audio_ok else "Generated_Silent_Audio",
                }
            ]

        self._write_legacy_live_alias()
        self._write_status_file(web_url, 0.0, 0.0)

        def log_web_ffmpeg_errors():
            if not proc.stderr:
                return
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self.log.warning("web ffmpeg: %s", line)

        threading.Thread(target=log_web_ffmpeg_errors, daemon=True).start()

    def play_web(self, web_config):
        web_url = (
            web_config.get("web_url")
            or web_config.get("url")
            or web_config.get("uri")
            or web_config.get("href")
        )

        if not web_url:
            raise RuntimeError(f"play_web called without web_url/url: {web_config}")

        self._start_web_hls_capture(web_url)

    # Compatibility with previous integrated URL-path attempt.
    def _build_webpage_ffmpeg_command(self, file_path, out_path, segment_pattern):
        web_url = self._web_url_for_path(file_path)
        audio_ok = self._start_hidden_browser(web_url)
        return self._build_web_capture_command(audio_ok, out_path, segment_pattern)

    def _start_webpage_renderer(self, url):
        return self._start_hidden_browser(url)

    # -------------------------------------------------------------------------
    # Media inspection
    # -------------------------------------------------------------------------

    def _get_duration(self, file_path):
        if self._is_webpage(file_path):
            return 0.0

        if not shutil.which("ffprobe"):
            return 0.0

        if str(file_path).startswith("http://") or str(file_path).startswith("https://"):
            return 0.0

        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )

            if result.returncode == 0:
                output = result.stdout.strip()
                if output:
                    return float(output)
        except Exception:
            pass

        return 0.0

    def _is_image(self, file_path):
        return str(file_path).lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))

    def _ffprobe_streams(self, file_path):
        if self._is_webpage(file_path):
            return []

        if not shutil.which("ffprobe"):
            return []

        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    file_path,
                ],
                text=True,
                capture_output=True,
                timeout=15,
            )

            if result.returncode != 0:
                return []

            parsed = json.loads(result.stdout)
            return parsed.get("streams", [])
        except Exception as e:
            self.log.warning("ffprobe stream check failed: %s", e)
            return []

    def _safe_hls_name(self, value, fallback):
        value = str(value or "").strip()
        if not value:
            value = fallback

        safe = []
        for ch in value:
            if ch.isalnum():
                safe.append(ch)
            elif ch in ("_", "-"):
                safe.append(ch)
            elif ch.isspace():
                safe.append("_")

        result = "".join(safe).strip("_")
        return result or fallback

    def _find_audio_tracks(self, file_path):
        tracks = []

        if self._is_webpage(file_path):
            return tracks

        if self._is_image(file_path) or not os.path.exists(file_path):
            return tracks

        for stream in self._ffprobe_streams(file_path):
            if stream.get("codec_type") != "audio":
                continue

            stream_index = stream.get("index")
            if stream_index is None:
                continue

            tags = stream.get("tags") or {}
            language = (tags.get("language") or "und").lower()
            title = tags.get("title") or ""
            codec = stream.get("codec_name") or ""
            channels = stream.get("channels")
            channel_layout = stream.get("channel_layout") or ""

            display_parts = []
            if language and language != "und":
                display_parts.append(language)
            if title:
                display_parts.append(title)
            if channel_layout:
                display_parts.append(channel_layout)
            elif channels:
                display_parts.append(f"{channels}ch")
            if codec:
                display_parts.append(codec)

            display_name = " ".join(display_parts).strip()
            if not display_name:
                display_name = f"Audio {len(tracks) + 1}"

            tracks.append(
                {
                    "track": len(tracks),
                    "stream_index": int(stream_index),
                    "language": language,
                    "title": title,
                    "codec": codec,
                    "channels": channels,
                    "channel_layout": channel_layout,
                    "display_name": display_name,
                    "hls_name": self._safe_hls_name(display_name, f"audio_{len(tracks)}"),
                }
            )

        return tracks

    def _find_text_subtitle_tracks(self, file_path):
        bitmap_codecs = {
            "hdmv_pgs_subtitle",
            "dvd_subtitle",
            "dvdsub",
            "dvb_subtitle",
            "xsub",
        }

        tracks = []

        if self._is_webpage(file_path):
            return tracks

        if self._is_image(file_path) or not os.path.exists(file_path):
            return tracks

        for stream in self._ffprobe_streams(file_path):
            if stream.get("codec_type") != "subtitle":
                continue

            codec = (stream.get("codec_name") or "").lower()
            if codec in bitmap_codecs:
                continue

            stream_index = stream.get("index")
            if stream_index is None:
                continue

            tags = stream.get("tags") or {}
            language = (tags.get("language") or "und").lower()
            title = tags.get("title") or ""

            display_parts = []
            if language and language != "und":
                display_parts.append(language)
            if title:
                display_parts.append(title)
            if codec:
                display_parts.append(codec)

            display_name = " ".join(display_parts).strip()
            if not display_name:
                display_name = f"Subtitle {len(tracks) + 1}"

            track_number = len(tracks)
            tracks.append(
                {
                    "track": track_number,
                    "stream_index": int(stream_index),
                    "language": language,
                    "title": title,
                    "codec": codec,
                    "display_name": display_name,
                    "file": f"subtitles_{track_number}.vtt",
                    "ready": False,
                }
            )

        return tracks

    # -------------------------------------------------------------------------
    # Subtitle extraction
    # -------------------------------------------------------------------------

    def _parse_vtt_timestamp(self, value):
        value = value.strip().replace(",", ".")
        parts = value.split(":")

        try:
            if len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                return hours * 3600 + minutes * 60 + seconds

            if len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                return minutes * 60 + seconds
        except Exception:
            return None

        return None

    def _format_vtt_timestamp(self, seconds):
        seconds = max(0.0, float(seconds))
        hours = int(seconds // 3600)
        seconds -= hours * 3600
        minutes = int(seconds // 60)
        seconds -= minutes * 60
        whole_seconds = int(seconds)
        millis = int(round((seconds - whole_seconds) * 1000))

        if millis >= 1000:
            whole_seconds += 1
            millis -= 1000

        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"

    def _collect_vtt_cues(self, raw_path):
        timestamp_re = re.compile(
            r"^\s*(?P<start>\d{1,2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})\s*-->\s*"
            r"(?P<end>\d{1,2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})(?P<settings>.*)$"
        )

        try:
            with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
                raw_text = f.read()
        except Exception as e:
            self.log.warning("Could not read raw subtitles: %s", e)
            return []

        raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.split(r"\n\s*\n", raw_text.strip())
        cues = []

        for block in blocks:
            lines = [line.rstrip("\n") for line in block.split("\n")]
            if not lines:
                continue

            first = lines[0].strip()
            if first.upper().startswith("WEBVTT"):
                continue
            if first.upper().startswith("STYLE"):
                continue
            if first.upper().startswith("NOTE"):
                continue
            if first.upper().startswith("REGION"):
                continue

            timing_index = None
            match = None
            for idx, line in enumerate(lines[:3]):
                match = timestamp_re.match(line)
                if match:
                    timing_index = idx
                    break

            if timing_index is None or match is None:
                continue

            start = self._parse_vtt_timestamp(match.group("start"))
            end = self._parse_vtt_timestamp(match.group("end"))
            settings = match.group("settings") or ""
            if start is None or end is None:
                continue

            cue_id_lines = []
            if timing_index > 0:
                cue_id_lines = [line for line in lines[:timing_index] if line.strip()]

            text_lines = lines[timing_index + 1:]
            cues.append(
                {
                    "start": start,
                    "end": end,
                    "settings": settings,
                    "cue_id_lines": cue_id_lines,
                    "text_lines": text_lines,
                }
            )

        return cues

    def _normalize_vtt_auto(self, raw_path, output_path, seek_seconds):
        cues = self._collect_vtt_cues(raw_path)
        if not cues:
            self.log.info("No subtitle cues found in raw VTT")
            return False

        first_start = min(cue["start"] for cue in cues)
        if seek_seconds > 30 and first_start > max(30.0, seek_seconds * 0.50):
            offset = float(seek_seconds)
            self.log.info("Subtitle timestamps look absolute; subtracting seek offset %.2f", offset)
        else:
            offset = 0.0
            self.log.info("Subtitle timestamps look stream-relative; not subtracting seek offset")

        output_blocks = ["WEBVTT", ""]
        kept = 0

        for cue in cues:
            new_start = cue["start"] - offset
            new_end = cue["end"] - offset

            if new_end <= 0:
                continue
            if new_start < 0:
                new_start = 0.0

            new_lines = []
            for cue_id_line in cue["cue_id_lines"]:
                if cue_id_line.strip():
                    new_lines.append(cue_id_line)

            new_lines.append(
                f"{self._format_vtt_timestamp(new_start)} --> {self._format_vtt_timestamp(new_end)}{cue['settings']}"
            )
            new_lines.extend(cue["text_lines"])
            output_blocks.append("\n".join(new_lines))
            output_blocks.append("")
            kept += 1

        if kept == 0:
            self.log.info("No subtitle cues remained after normalization")
            return False

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(output_blocks).strip() + "\n")
        except Exception as e:
            self.log.warning("Could not write normalized subtitles: %s", e)
            return False

        self.log.info("Normalized %d subtitle cues for stream window", kept)
        return True

    def _start_subtitle_extraction_async(self, file_path, seek_seconds, generation):
        if self._is_webpage(file_path):
            with self.lock:
                self.subtitles_state = "none"
                self.subtitle_generation = int(time.time() * 1000)
                self.subtitle_tracks = []
            self._write_status_file(file_path, seek_seconds, self.duration)
            return

        with self.lock:
            self.subtitles_state = "pending"
            self.subtitle_generation = int(time.time() * 1000)
            self.subtitle_tracks = []

        self._write_status_file(file_path, seek_seconds, self.duration)

        thread = threading.Thread(
            target=self._extract_all_subtitles_worker,
            args=(file_path, seek_seconds, generation),
            daemon=True,
        )
        self.subtitle_thread = thread
        thread.start()

    def _extract_one_subtitle_track(self, file_path, seek_seconds, track_info, generation):
        track_number = track_info["track"]
        stream_index = track_info["stream_index"]
        subtitle_path = self._subtitle_track_path(track_number)
        raw_path = self._raw_subtitle_track_path(track_number)

        try:
            if os.path.exists(subtitle_path):
                os.unlink(subtitle_path)
            if os.path.exists(raw_path):
                os.unlink(raw_path)
        except Exception:
            pass

        duration_remaining = 0.0
        if self.duration > 0:
            duration_remaining = max(0.0, self.duration - float(seek_seconds or 0.0))

        subtitle_window = self.subtitle_window_seconds
        if duration_remaining > 0:
            subtitle_window = min(self.subtitle_window_seconds, duration_remaining + 10.0)
        subtitle_window = max(15.0, subtitle_window)

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-ss",
            str(float(seek_seconds or 0.0)),
            "-t",
            str(subtitle_window),
            "-i",
            file_path,
            "-map",
            f"0:{stream_index}",
            "-c:s",
            "webvtt",
            raw_path,
        ]

        self.log.info(
            "Extracting subtitle track %s stream %s to %s from %.2fs for %.2fs",
            track_number,
            stream_index,
            raw_path,
            float(seek_seconds or 0.0),
            subtitle_window,
        )

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.subtitle_extract_timeout,
            )

            with self.lock:
                if generation != self.stream_generation:
                    self.log.info("Discarding subtitles for stale stream generation")
                    return False

            if result.returncode != 0:
                self.log.warning("Subtitle extraction failed for track %s", track_number)
                if result.stderr:
                    self.log.warning("subtitle ffmpeg: %s", result.stderr.strip())
                return False

            if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
                self.log.info("Subtitle extraction produced no output for track %s", track_number)
                return False

            if self._normalize_vtt_auto(raw_path, subtitle_path, seek_seconds):
                self.log.info("Subtitle track %s extraction complete: %s", track_number, subtitle_path)
                return True

            return False

        except subprocess.TimeoutExpired:
            self.log.warning(
                "Subtitle track %s extraction timed out after %.1fs; video stream continues",
                track_number,
                self.subtitle_extract_timeout,
            )
            return False

        except Exception as e:
            self.log.warning("Subtitle track %s extraction error: %s", track_number, e)
            return False

    def _extract_all_subtitles_worker(self, file_path, seek_seconds, generation):
        self._remove_subtitles_file()

        if self._is_image(file_path) or not os.path.exists(file_path):
            with self.lock:
                if generation == self.stream_generation:
                    self.subtitles_state = "none"
                    self.subtitle_tracks = []
                    self.subtitle_generation = int(time.time() * 1000)
            self._write_status_file(file_path, seek_seconds, self.duration)
            return

        tracks = self._find_text_subtitle_tracks(file_path)
        if not tracks:
            self.log.info("No compatible text subtitle streams found")
            with self.lock:
                if generation == self.stream_generation:
                    self.subtitles_state = "none"
                    self.subtitle_tracks = []
                    self.subtitle_generation = int(time.time() * 1000)
            self._write_status_file(file_path, seek_seconds, self.duration)
            return

        with self.lock:
            if generation == self.stream_generation:
                self.subtitle_tracks = tracks
                self.subtitles_state = "pending"
                self.subtitle_generation = int(time.time() * 1000)

        self._write_status_file(file_path, seek_seconds, self.duration)
        ready_count = 0

        for track in tracks:
            with self.lock:
                if generation != self.stream_generation:
                    self.log.info("Stopping subtitle extraction for stale stream generation")
                    return

            ok = self._extract_one_subtitle_track(file_path, seek_seconds, track, generation)
            if ok:
                ready_count += 1
                track["ready"] = True
                if not os.path.exists(self._subtitle_path()):
                    try:
                        shutil.copyfile(self._subtitle_track_path(track["track"]), self._subtitle_path())
                    except Exception:
                        pass
            else:
                track["ready"] = False

            with self.lock:
                if generation == self.stream_generation:
                    self.subtitle_tracks = tracks
                    self.subtitles_state = "ready" if ready_count > 0 else "pending"
                    self.subtitle_generation = int(time.time() * 1000)
            self._write_status_file(file_path, seek_seconds, self.duration)

        with self.lock:
            if generation == self.stream_generation:
                self.subtitles_state = "ready" if ready_count > 0 else "failed"
                self.subtitle_tracks = tracks
                self.subtitle_generation = int(time.time() * 1000)

        self._write_status_file(file_path, seek_seconds, self.duration)

    # -------------------------------------------------------------------------
    # FFmpeg command builders
    # -------------------------------------------------------------------------

    def _build_file_ffmpeg_command(self, file_path, seek_seconds, out_path, segment_pattern):
        """
        Normal video/image streaming path.

        Web capture does not use this path, so Brave/Chromium/Xvfb cannot affect
        normal movie/video/image files.
        """
        is_image = self._is_image(file_path)
        audio_tracks = self._find_audio_tracks(file_path)

        if not audio_tracks:
            audio_tracks = [
                {
                    "track": 0,
                    "stream_index": None,
                    "language": "und",
                    "title": "",
                    "codec": "generated",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "display_name": "Generated Silent Audio",
                    "hls_name": "Generated_Silent_Audio",
                }
            ]

        with self.lock:
            self.audio_tracks = audio_tracks

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+discardcorrupt",
            "-err_detect",
            "ignore_err",
            "-y",
        ]

        if is_image:
            cmd += [
                "-loop",
                "1",
                "-framerate",
                "30",
                "-i",
                file_path,
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]

            with self.lock:
                self.audio_tracks = [
                    {
                        "track": 0,
                        "stream_index": None,
                        "language": "und",
                        "title": "",
                        "codec": "generated",
                        "channels": 2,
                        "channel_layout": "stereo",
                        "display_name": "Generated Silent Audio",
                        "hls_name": "Generated_Silent_Audio",
                    }
                ]
        else:
            if seek_seconds > 0:
                cmd += ["-ss", str(seek_seconds)]

            cmd += [
                "-re",
                "-analyzeduration",
                "200M",
                "-probesize",
                "200M",
                "-i",
                file_path,
                "-map",
                "0:V:0",
            ]

            real_audio_count = 0
            for track in self.audio_tracks:
                if track["stream_index"] is not None:
                    real_audio_count += 1
                    cmd += ["-map", f"0:{track['stream_index']}"]

            if real_audio_count == 0:
                cmd += [
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",
                    "-map",
                    "1:a:0",
                ]

            cmd += [
                "-dn",
                "-sn",
                "-avoid_negative_ts",
                "make_zero",
            ]

        var_parts = ["v:0,agroup:audios"]
        for i, track in enumerate(self.audio_tracks):
            language = self._safe_hls_name(track.get("language") or "und", "und")
            name = self._safe_hls_name(track.get("display_name") or f"Audio_{i + 1}", f"Audio_{i + 1}")
            audio_part = f"a:{i},agroup:audios,language:{language},name:{name}"
            if i == 0:
                audio_part += ",default:yes"
            var_parts.append(audio_part)

        var_stream_map = " ".join(var_parts)

        cmd += [
            "-vf",
            "scale={}:{}:force_original_aspect_ratio=decrease:flags=bicubic,pad={}:{}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p".format(
                self.width,
                self.height,
                self.width,
                self.height,
            ),
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            self.preset,
            "-profile:v",
            "main",
            "-level:v",
            "3.1",
            "-tune",
            "zerolatency",
            "-b:v",
            self.video_bitrate,
            "-maxrate",
            self.video_bitrate,
            "-bufsize",
            "5000k",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-max_muxing_queue_size",
            "4096",
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
            "-hls_delete_threshold",
            self.hls_delete_threshold,
            "-hls_flags",
            "delete_segments+omit_endlist+independent_segments+program_date_time",
            "-hls_allow_cache",
            "0",
            "-start_number",
            str(int(time.time())),
            "-reset_timestamps",
            "1",
            "-master_pl_name",
            "master.m3u8",
            "-var_stream_map",
            var_stream_map,
            "-hls_segment_filename",
            segment_pattern,
            out_path,
        ]

        self.log.info("Audio tracks for HLS master: %s", json.dumps(self.audio_tracks))
        self.log.info("FFmpeg var_stream_map: %s", var_stream_map)
        return cmd

    # Backward-compatible method name.
    def _build_ffmpeg_command(self, file_path, seek_seconds, out_path, segment_pattern):
        return self._build_file_ffmpeg_command(file_path, seek_seconds, out_path, segment_pattern)

    # -------------------------------------------------------------------------
    # Stream start
    # -------------------------------------------------------------------------

    def _start_stream(self, file_path, seek_seconds=0.0):
        if self._is_webpage(file_path):
            web_url = self._web_url_for_path(file_path)
            self._start_web_hls_capture(web_url)
            return

        self._stop_ffmpeg()
        self._prepare_stream_dir(clear=True)
        self._start_http_server()

        seek_seconds = float(seek_seconds or 0.0)
        duration = self._get_duration(file_path)

        with self.lock:
            self.current_path = file_path
            self.seek_offset = seek_seconds
            self.duration = duration
            self.stream_generation = int(time.time() * 1000)
            self.stream_started_at = time.time()
            self.subtitle_generation = 0
            self.subtitles_state = "pending"
            self.audio_tracks = []
            self.subtitle_tracks = []
            current_generation = self.stream_generation

        out_path = os.path.join(self.stream_dir, "stream_%v.m3u8")
        segment_pattern = os.path.join(self.stream_dir, "stream_%v_%05d.ts")
        cmd = self._build_file_ffmpeg_command(
            file_path=file_path,
            seek_seconds=seek_seconds,
            out_path=out_path,
            segment_pattern=segment_pattern,
        )

        self.log.info("Starting FFmpeg direct HLS stream")
        self.log.info("Input: %s", file_path)
        self.log.info("Seek: %.2f", seek_seconds)
        self.log.info("Duration: %.2f", duration)
        self.log.info("Output master: %s", os.path.join(self.stream_dir, "master.m3u8"))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )

        with self.lock:
            self.proc = proc
            self.started_at = time.time()

        self._write_legacy_live_alias()
        self._write_status_file(file_path, seek_seconds, duration)
        self._start_subtitle_extraction_async(file_path, seek_seconds, current_generation)

        def log_ffmpeg_errors():
            if not proc.stderr:
                return
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self.log.warning("ffmpeg: %s", line)

        threading.Thread(target=log_ffmpeg_errors, daemon=True).start()