#!/usr/bin/env bash

set -u

###############################################################################
# FieldStation42 Fire TV Streaming Launcher
#
# Starts:
#   1. Xvfb hidden display
#   2. hidden MPV on DISPLAY=:42
#   3. FFmpeg HLS encoder
#   4. HTTP server for live.m3u8
#   5. FieldStation42 field_player.py
#
# Usage:
#   ./start_fs42_firetv.sh start
#   ./start_fs42_firetv.sh stop
#   ./start_fs42_firetv.sh restart
#   ./start_fs42_firetv.sh status
###############################################################################

# ---- CHANGE THESE IF NEEDED --------------------------------------------------

FS42_DIR="/tank/open/Media/FieldStation42_firetv_stream_patch/FieldStation42-main"

DISPLAY_NUM=":42"
VIDEO_SIZE="1280x720"
STREAM_DIR="/tmp/fs42-hls"
HLS_PORT="8080"
MPV_SOCKET="/tmp/mpvsocket"

# Use this unless you set up a PulseAudio null sink.
AUDIO_INPUT="default"

# If you want to use a PulseAudio null sink later, set:
# AUDIO_INPUT="fs42sink.monitor"

# Optional: create/use a PulseAudio null sink for cleaner audio routing.
# Set to "yes" if you want the script to create fs42sink.
USE_PULSE_NULL_SINK="no"
PULSE_SINK_NAME="fs42sink"

# -----------------------------------------------------------------------------


die() {
    echo "ERROR: $*" >&2
    exit 1
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Missing command: $1"
}

check_requirements() {
    need_cmd terminator
    need_cmd Xvfb
    need_cmd mpv
    need_cmd ffmpeg
    need_cmd python3

    if [ ! -d "$FS42_DIR" ]; then
        die "FS42_DIR does not exist: $FS42_DIR"
    fi

    if [ ! -f "$FS42_DIR/field_player.py" ]; then
        die "field_player.py not found in: $FS42_DIR"
    fi
}

check_config_hint() {
    echo
    echo "Checking FieldStation42 MPV config..."

    if grep -R '"start_mpv"[[:space:]]*:[[:space:]]*false' "$FS42_DIR/confs" >/dev/null 2>&1; then
        echo "OK: Found start_mpv false somewhere under confs/"
    else
        echo
        echo "WARNING:"
        echo "I did not find:"
        echo '  "start_mpv": false'
        echo
        echo "You probably need this in your active confs/main_config.json"
        echo "or FS42 may launch a visible MPV window."
        echo
    fi
}

setup_stream_dir() {
    rm -rf "$STREAM_DIR"
    mkdir -p "$STREAM_DIR"
}

setup_pulse_sink() {
    if [ "$USE_PULSE_NULL_SINK" = "yes" ]; then
        echo "Setting up PulseAudio null sink: $PULSE_SINK_NAME"

        if pactl list short sinks 2>/dev/null | grep -q "$PULSE_SINK_NAME"; then
            echo "Pulse sink already exists: $PULSE_SINK_NAME"
        else
            pactl load-module module-null-sink \
                sink_name="$PULSE_SINK_NAME" \
                sink_properties=device.description="$PULSE_SINK_NAME" >/dev/null 2>&1 || true
        fi

        AUDIO_INPUT="${PULSE_SINK_NAME}.monitor"
    fi
}

open_term() {
    local title="$1"
    local cmd="$2"

    terminator \
        --title="$title" \
        --working-directory="$FS42_DIR" \
        -e "bash -lc '$cmd; echo; echo \"[$title exited]\"; echo \"Press Enter to close...\"; read -r'" &
}

start_all() {
    check_requirements
    check_config_hint
    setup_stream_dir
    setup_pulse_sink

    echo "Stopping stale processes first..."
    stop_all_quiet

    rm -f "$MPV_SOCKET"
    setup_stream_dir

    echo "Starting Fire TV streaming stack in Terminator windows..."
    echo

    open_term "FS42 1 - Xvfb hidden display" \
        "echo Starting Xvfb on $DISPLAY_NUM; Xvfb $DISPLAY_NUM -screen 0 ${VIDEO_SIZE}x24 -ac +extension GLX +render -noreset"

    sleep 2

    if [ "$USE_PULSE_NULL_SINK" = "yes" ]; then
        MPV_AUDIO_ENV="PULSE_SINK=$PULSE_SINK_NAME"
    else
        MPV_AUDIO_ENV=""
    fi

    open_term "FS42 2 - hidden MPV" \
        "echo Starting hidden MPV on DISPLAY=$DISPLAY_NUM; DISPLAY=$DISPLAY_NUM $MPV_AUDIO_ENV mpv --input-ipc-server=$MPV_SOCKET --idle=yes --force-window=immediate --no-terminal --geometry=${VIDEO_SIZE}+0+0 --autofit=$VIDEO_SIZE --no-osc --no-border --really-quiet"

    echo "Waiting for MPV socket..."
    for i in $(seq 1 20); do
        if [ -S "$MPV_SOCKET" ]; then
            echo "MPV socket ready: $MPV_SOCKET"
            break
        fi
        sleep 1
    done

    if [ ! -S "$MPV_SOCKET" ]; then
        echo "WARNING: MPV socket was not created yet: $MPV_SOCKET"
        echo "FS42 may not attach to hidden MPV."
    fi

    open_term "FS42 3 - FFmpeg HLS encoder" \
        "echo Starting FFmpeg HLS encoder; ffmpeg -hide_banner -loglevel info -f x11grab -draw_mouse 0 -framerate 30 -video_size $VIDEO_SIZE -i $DISPLAY_NUM.0 -f pulse -i $AUDIO_INPUT -c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -c:a aac -b:a 128k -f hls -hls_time 2 -hls_list_size 5 -hls_flags delete_segments $STREAM_DIR/live.m3u8"

    sleep 2

    open_term "FS42 4 - HLS HTTP server" \
        "echo Serving HLS from $STREAM_DIR on port $HLS_PORT; cd $STREAM_DIR && python3 -m http.server $HLS_PORT"

    sleep 2

    open_term "FS42 5 - FieldStation42" \
        "echo Starting FieldStation42; cd $FS42_DIR && python3 field_player.py"

    echo
    echo "Started."
    echo
    echo "HLS stream URL:"
    echo "  http://YOUR_SERVER_IP:$HLS_PORT/live.m3u8"
    echo
    echo "Test locally with:"
    echo "  curl http://127.0.0.1:$HLS_PORT/live.m3u8"
    echo
    echo "Check status with:"
    echo "  ./start_fs42_firetv.sh status"
}

stop_all_quiet() {
    pkill -f "field_player.py" >/dev/null 2>&1 || true
    pkill -f "python3 -m http.server $HLS_PORT" >/dev/null 2>&1 || true
    pkill -f "ffmpeg.*$STREAM_DIR/live.m3u8" >/dev/null 2>&1 || true
    pkill -f "mpv.*$MPV_SOCKET" >/dev/null 2>&1 || true
    pkill -f "Xvfb $DISPLAY_NUM" >/dev/null 2>&1 || true
}

stop_all() {
    echo "Stopping FieldStation42 Fire TV streaming stack..."

    stop_all_quiet

    rm -f "$MPV_SOCKET"
    rm -rf "$STREAM_DIR"

    echo "Stopped."
}

status_all() {
    echo
    echo "Processes:"
    echo "----------"
    ps aux | grep -E "[X]vfb $DISPLAY_NUM|[m]pv.*$MPV_SOCKET|[f]fmpeg.*$STREAM_DIR|[p]ython3 -m http.server $HLS_PORT|[f]ield_player.py" || true

    echo
    echo "MPV socket:"
    echo "-----------"
    if [ -S "$MPV_SOCKET" ]; then
        ls -l "$MPV_SOCKET"
    else
        echo "Missing: $MPV_SOCKET"
    fi

    echo
    echo "HLS files:"
    echo "----------"
    if [ -d "$STREAM_DIR" ]; then
        ls -lh "$STREAM_DIR"
    else
        echo "Missing stream dir: $STREAM_DIR"
    fi

    echo
    echo "Local HLS test:"
    echo "---------------"
    if command -v curl >/dev/null 2>&1; then
        curl -s --max-time 3 "http://127.0.0.1:$HLS_PORT/live.m3u8" | head -20 || true
    else
        echo "curl not installed"
    fi

    echo
}

case "${1:-start}" in
    start)
        start_all
        ;;
    stop)
        stop_all
        ;;
    restart)
        stop_all
        sleep 2
        start_all
        ;;
    status)
        status_all
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
