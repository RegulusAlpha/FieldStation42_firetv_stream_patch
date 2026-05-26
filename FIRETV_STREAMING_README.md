# Fire TV Streaming Mode for FieldStation42

This patched version adds an optional **headless Fire TV streaming mode**.

FieldStation42 still controls `mpv`, but `mpv` is rendered on a hidden Xvfb display instead of a visible server window. `ffmpeg` captures that hidden display plus audio and writes an HLS stream that your Fire TV receiver app can play.

## Architecture

```text
FieldStation42 field_player.py
  -> mpv on hidden Xvfb display :42
  -> ffmpeg captures :42.0 and audio
  -> HLS files in runtime/firetv_hls
  -> built-in HTTP server on port 8080
  -> Fire TV app plays http://SERVER_IP:8080/live.m3u8
```

FieldStation42's normal API remains on port `4242`, so the Fire TV app can still call:

```text
http://SERVER_IP:4242/player/channels/up
http://SERVER_IP:4242/player/channels/down
http://SERVER_IP:4242/player/status
```

## Install system packages

On Ubuntu/Debian/Raspberry Pi OS, install:

```bash
sudo apt update
sudo apt install xvfb ffmpeg pulseaudio-utils
```

If your system uses PipeWire, `pactl` usually still works through the PulseAudio-compatible PipeWire service.

## Enable in `confs/main_config.json`

Create or edit:

```text
confs/main_config.json
```

Add:

```json
{
  "server_host": "0.0.0.0",
  "server_port": 4242,
  "start_mpv": true,
  "firetv_stream": {
    "enabled": true,
    "display": ":42",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "output_dir": "runtime/firetv_hls",
    "playlist_name": "live.m3u8",
    "http_host": "0.0.0.0",
    "http_port": 8080,
    "video_bitrate": "2800k",
    "audio_bitrate": "128k",
    "hls_time": 2,
    "hls_list_size": 5,
    "setup_audio_sink": true,
    "audio_sink_name": "fs42sink"
  }
}
```

If you already have a `main_config.json`, merge the `firetv_stream` block into your existing JSON instead of replacing the whole file.

## Run

```bash
python3 field_player.py
```

After a few seconds, test from another computer:

```bash
curl http://SERVER_IP:8080/live.m3u8
curl http://SERVER_IP:4242/player/status
```

The playlist should start with `#EXTM3U`.

## Fire TV receiver URL

Set the Fire TV app's HLS URL/host to your FieldStation42 server IP. The stream is:

```text
http://SERVER_IP:8080/live.m3u8
```

## Troubleshooting

### No HLS playlist

Check if `ffmpeg` is running:

```bash
ps aux | grep ffmpeg
```

Check whether files are being written:

```bash
ls -lh runtime/firetv_hls
```

### MPV still appears on the server

Make sure `firetv_stream.enabled` is true and `start_mpv` is true. This patch sets `DISPLAY=:42` before FieldStation42 launches `mpv`, so MPV should render into Xvfb instead of the visible desktop.

### No audio

Try disabling the custom audio sink and using the default PulseAudio monitor/input:

```json
"firetv_stream": {
  "enabled": true,
  "setup_audio_sink": false,
  "audio_input": "default"
}
```

Or inspect PulseAudio sources:

```bash
pactl list short sources
```

Then set `audio_input` to the correct source name.

### Need lower latency

Try smaller HLS segments:

```json
"hls_time": 1,
"hls_list_size": 3
```

This may increase CPU usage and make playback less stable on slower hardware.

## Files changed by this patch

- Added `fs42/firetv_stream.py`
- Updated `fs42/station_manager.py` so `main_config.json` can load `firetv_stream`
- Updated `field_player.py` to start/stop the streaming helper when enabled
- Added this README
