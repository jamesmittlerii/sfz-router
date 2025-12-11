#!/usr/bin/env python3
"""
sfz-router.py

- Launches a single sfizz_jack instance.
- Wires MIDI + audio via JACK.
- Listens to the SL88 Studio Mk2 on the "SL CTRL" ALSA port.
- On Program Change from the COMMON CHANNEL (16 -> MIDI channel 15),
  loads the corresponding SFZ file into sfizz via its stdin command interface.
- Remembers the last-used program and forces the SL88 to that program on startup.
  If there's no history yet, it defaults to P011 (program 10).
- Debounces rapid Program Change events so spinning the selector quickly
  only loads the final resting program.
"""

import os
import subprocess
import sys
import time

import rtmidi

# ---- USER CONFIG ---------------------------------------------------------

# Common MIDI channel used by SL88 as COMMON CHANNEL (1–16)
COMMON_CHANNEL = 1  # SL shows 1, MIDI uses channel 0 (0-based)

# Program -> SFZ mapping (MIDI program numbers, 0–127)
SFZ_MAP = {
    10: "/root/sfz/Wurlitzer/Wurlitzer.sfz",
    11: "/root/sfz/Clavinet/Clavinet.sfz",
    12: "/root/sfz/K18-Upright-Piano/K18-Upright-Piano.sfz",
    13: "/root/sfz/SalamanderGrandPianoV6/sfz_daw/Accurate-SalamanderGrandPiano_flat.Recommended.sfz",
    14: "/root/sfz/GregSullivan.E-Pianos-master/PianetT/PianetT.sfz",
    15: "/root/sfz/GregSullivan.E-Pianos-master/EP200/EP200.sfz",
    16: "/root/sfz/jRhodes3d/jlearman.jRhodes3d-master/jRhodes3d-st/_jRhodes3d-st-flac.sfz",
}

# Path to sfizz_jack binary
SFIZZ_BIN = "/usr/local/bin/sfizz_jack"

# JACK client name for sfizz
SFIZZ_CLIENT_NAME = "sfizz-sl88"

# File where we remember the last-used MIDI program number
LAST_PROG_FILE = "/root/sfz/last_program.txt"

# How long to wait after the last Program Change before actually loading the SFZ
DEBOUNCE_SECONDS = 0.50  # 500 ms; tweak to taste

# -------------------------------------------------------------------------


def log(msg: str) -> None:
    """Simple print logger with flush."""
    print(msg, flush=True)


def load_last_program() -> int:
    """
    Load the last-used program from disk.
    If not present or invalid, default to program 10 (P011).
    """
    default_prog = 10  # P011
    if not os.path.exists(LAST_PROG_FILE):
        return default_prog
    try:
        with open(LAST_PROG_FILE, "r") as f:
            val = int(f.read().strip())
        if val in SFZ_MAP:
            return val
        return default_prog
    except Exception as e:
        log(f"[sfizz-router] Failed to read {LAST_PROG_FILE}: {e}")
        return default_prog


def save_last_program(program: int) -> None:
    """Persist the last-used program number to disk."""
    try:
        with open(LAST_PROG_FILE, "w") as f:
            f.write(str(program))
    except Exception as e:
        log(f"[sfizz-router] Failed to save last program: {e}")


def start_sfizz(default_program: int) -> subprocess.Popen:
    """
    Start sfizz_jack and return the subprocess.
    Optionally load a default SFZ based on default_program.
    """
    default_sfz = SFZ_MAP.get(default_program) or next(iter(SFZ_MAP.values()))

    cmd = [
        SFIZZ_BIN,
        "--client_name",
        SFIZZ_CLIENT_NAME,
        "--jack_autoconnect",
        default_sfz,
    ]

    log(f"[sfizz-router] Starting sfizz: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def jack_autoconnect():
    """
    Wire MIDI and audio in JACK:

    - system:midi_capture_* -> sfizz-sl88:input
    - sfizz-sl88:output_1   -> system:playback_1
    - sfizz-sl88:output_2   -> system:playback_2
    """
    # Give JACK and sfizz a moment to expose ports
    time.sleep(1.0)

    # MIDI sources: same as your old jack-autoconnect.sh
    midi_sources = [
        "system:midi_capture_1",
        "system:midi_capture_2",
        "system:midi_capture_3",
        "system:midi_capture_4",
    ]
    midi_dest = f"{SFIZZ_CLIENT_NAME}:input"

    for src in midi_sources:
        try:
            subprocess.run(
                ["jack_connect", src, midi_dest],
                stderr=subprocess.DEVNULL,
            )
            log(f"[sfizz-router] jack_connect {src} -> {midi_dest}")
        except Exception as e:
            log(f"[sfizz-router] jack_connect MIDI failed for {src}: {e}")

    # AUDIO
    audio_pairs = [
        (f"{SFIZZ_CLIENT_NAME}:output_1", "system:playback_1"),
        (f"{SFIZZ_CLIENT_NAME}:output_2", "system:playback_2"),
    ]

    for src, dest in audio_pairs:
        try:
            subprocess.run(
                ["jack_connect", src, dest],
                stderr=subprocess.DEVNULL,
            )
            log(f"[sfizz-router] jack_connect {src} -> {dest}")
        except Exception as e:
            log(f"[sfizz-router] jack_connect AUDIO failed for {src}: {e}")


def open_sl_ctrl_in():
    """
    Find and open the SL CTRL ALSA MIDI input port.

    Returns (midi_in, port_name).
    """
    midiin = rtmidi.MidiIn()
    ports = midiin.get_ports()

    if not ports:
        raise RuntimeError("No ALSA MIDI ports found.")

    # Try exact 'SL CTRL' match first
    for idx, name in enumerate(ports):
        if "SL CTRL" in name:
            midiin.open_port(idx)
            log(f"[sfizz-router] Opened MIDI IN: {name} (index {idx})")
            return midiin, name

    # Fallback: any port containing 'SL'
    for idx, name in enumerate(ports):
        if "SL" in name:
            midiin.open_port(idx)
            log(f"[sfizz-router] Opened MIDI IN (fallback): {name} (index {idx})")
            return midiin, name

    raise RuntimeError("Could not find an SL CTRL or SL* MIDI IN port.")


def open_sl_ctrl_out():
    """
    Find and open the SL CTRL ALSA MIDI output port.

    Returns (midi_out, port_name).
    """
    midiout = rtmidi.MidiOut()
    ports = midiout.get_ports()

    if not ports:
        raise RuntimeError("No ALSA MIDI OUT ports found.")

    for idx, name in enumerate(ports):
        if "SL CTRL" in name:
            midiout.open_port(idx)
            log(f"[sfizz-router] Opened MIDI OUT: {name} (index {idx})")
            return midiout, name

    for idx, name in enumerate(ports):
        if "SL" in name:
            midiout.open_port(idx)
            log(f"[sfizz-router] Opened MIDI OUT (fallback): {name} (index {idx})")
            return midiout, name

    raise RuntimeError("Could not find an SL CTRL or SL* MIDI OUT port.")


def force_initial_program(midiout, program: int) -> None:
    """
    Send a Program Change on the COMMON channel to force the SL88
    to the given program (e.g. 10 -> P011).
    """
    common_midi_channel = COMMON_CHANNEL - 1  # 16 -> 15
    status = 0xC0 | common_midi_channel       # Program Change on that channel
    msg = [status, program]
    midiout.send_message(msg)
    log(f"[sfizz-router] Forced SL88 to program={program} (P{program+1:03d}) on ch={COMMON_CHANNEL}")


def main():
    try:
        midiin, in_name = open_sl_ctrl_in()
        midiout, out_name = open_sl_ctrl_out()
    except Exception as e:
        log(f"[sfizz-router] ERROR opening MIDI ports: {e}")
        sys.exit(1)

    # Load last-used program (or default to 10 = P011)
    default_prog = load_last_program()
    log(f"[sfizz-router] Last program from disk (or default) = {default_prog}")

    # Force the SL88 to that program so keyboard + sfizz are in sync
    force_initial_program(midiout, default_prog)

    # Start sfizz with the same program
    sfizz = start_sfizz(default_prog)

    # Wire JACK MIDI + audio
    jack_autoconnect()

    sfizz_stdout = sfizz.stdout
    log("[sfizz-router] Ready. Listening for Program Change events...")

    common_midi_channel = COMMON_CHANNEL - 1  # convert 1–16 to 0–15

    # State for debounce
    current_program = default_prog          # what sfizz is actually using
    last_requested_program = default_prog   # last program we *saw* from the SL88
    last_request_time = time.monotonic()    # when we last saw a Program Change

    try:
        while True:
            msg = midiin.get_message()
            if msg:
                data, _delta = msg
                status = data[0]
                status_type = status & 0xF0
                channel = status & 0x0F

                # Program Change from SL88 on the COMMON channel
                if status_type == 0xC0 and channel == common_midi_channel:
                    program = data[1]
                    log(f"[sfizz-router] Program Change received on ch={channel+1}, program={program}")

                    # Update debounce state only; don't load SFZ yet
                    last_requested_program = program
                    last_request_time = time.monotonic()

            # Debounce check: if we've waited long enough since the last Program Change,
            # and it's different from what sfizz is currently using, then switch.
            now = time.monotonic()
            if (
                last_requested_program in SFZ_MAP
                and last_requested_program != current_program
                and (now - last_request_time) >= DEBOUNCE_SECONDS
            ):
                program = last_requested_program
                sfz_path = SFZ_MAP[program]

                log(f"[sfizz-router] Debounced load: program={program}, SFZ={sfz_path}")
                save_last_program(program)

                cmd = f"load_instrument {sfz_path}\n"
                try:
                    sfizz.stdin.write(cmd)
                    sfizz.stdin.flush()
                    current_program = program
                except Exception as e:
                    log(f"[sfizz-router] ERROR sending load_instrument to sfizz: {e}")

            # Small sleep to avoid busy-wait
            time.sleep(0.005)

    except KeyboardInterrupt:
        log("[sfizz-router] KeyboardInterrupt, shutting down...")

    finally:
        try:
            midiin.close_port()
        except Exception:
            pass

        try:
            midiout.close_port()
        except Exception:
            pass

        if sfizz and sfizz.poll() is None:
            log("[sfizz-router] Terminating sfizz process...")
            try:
                sfizz.terminate()
                try:
                    sfizz.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log("[sfizz-router] sfizz did not exit, killing...")
                    sfizz.kill()
            except Exception as e:
                log(f"[sfizz-router] ERROR stopping sfizz: {e}")


if __name__ == "__main__":
    main()

