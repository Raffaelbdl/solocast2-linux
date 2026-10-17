#!/usr/bin/env python3
"""Configure a HyperX SoloCast 2 (USB 03f0:0fbf) from Linux.

The mic's onboard DSP settings (high/low-pass filters, presence boost) are set
through its vendor HID interface. Gain and mute are standard USB Audio controls,
exposed by ALSA, so they go through `amixer`.

HID protocol (64-byte reports, first byte is the report ID):

    60 01 00 00 00 FF ...         set filters, FF = bit0 high-pass | bit1 low-pass
    60 02                         read settings -> 61 02 .. .. .. .. .. FF ...
    66 01 01 00 00 00 G T 00 00 00 ...
                                  set presence boost band:
                                  G = 0x80 if negative | whole dB, T = tenths
    66 02 00 00 00                read EQ -> 67 02 00 00 00 00 G T Fl Fh type ...

Every SET is acknowledged with: FF 01 ... (byte 14, 15 = echoed command).
"""
import argparse
import glob
import os
import re
import select
import subprocess
import sys
import time

VID, PID = 0x03F0, 0x0FBF
REPORT_LEN = 64

SETTINGS = 0x60  # generic audio settings, answered on 0x61
ONBOARD_EQ = 0x66  # onboard EQ, answered on 0x67
SET, GET = 0x01, 0x02
ACK = 0xFF
ACK_OK = 0x01

HIGH_PASS, LOW_PASS = 0x01, 0x02

BOOST_MIN, BOOST_MAX, BOOST_STEP = -6.0, 6.0, 0.5
BOOST_FREQUENCY_HZ = 3000  # centre of the presence band, as the mic reports it

ALSA_VOLUME = "Mic Capture Volume"
ALSA_SWITCH = "Mic Capture Switch"


class DeviceError(Exception):
    pass


def find_hidraw():
    """Return the /dev/hidrawN node of the first SoloCast 2."""
    hid_id = f"HID_ID=0003:{VID:08X}:{PID:08X}"
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device/uevent")) as f:
                if hid_id in f.read():
                    return "/dev/" + os.path.basename(node)
        except OSError:
            continue
    raise DeviceError("HyperX SoloCast 2 not found (is it plugged in?)")


def find_alsa_card():
    """Return the ALSA card index of the first SoloCast 2."""
    usb_id = f"{VID:04x}:{PID:04x}"
    for path in sorted(glob.glob("/proc/asound/card*/usbid")):
        with open(path) as f:
            if f.read().strip() == usb_id:
                return int(re.search(r"card(\d+)", path).group(1))
    raise DeviceError("HyperX SoloCast 2 audio card not found")


def encode_gain(db):
    whole = int(abs(db))
    tenths = round(abs(db) * 10) % 10
    return [(0x80 if db < 0 else 0) | whole, tenths]


def decode_gain(b0, b1):
    value = (b0 & 0x7F) + b1 / 10
    return -value if b0 & 0x80 else value


class SoloCast2:
    """Onboard DSP settings over HID."""

    def __init__(self, path=None, timeout=0.5):
        self.path = path or find_hidraw()
        self.timeout = timeout
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(
                f"No permission to open {self.path}. Install udev/70-hyperx-solocast2.rules "
                "(see README.md), then replug the mic."
            ) from None

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _drain(self):
        while select.select([self.fd], [], [], 0)[0]:
            os.read(self.fd, REPORT_LEN)

    def _transact(self, payload, accept):
        try:
            self._drain()
            os.write(self.fd, bytes(payload).ljust(REPORT_LEN, b"\0"))
            deadline = time.monotonic() + self.timeout
            while (left := deadline - time.monotonic()) > 0:
                if not select.select([self.fd], [], [], left)[0]:
                    break
                report = os.read(self.fd, REPORT_LEN)
                if accept(report):
                    return report
        except OSError as e:
            raise DeviceError(f"lost contact with the mic ({e.strerror})") from None
        raise DeviceError(f"no reply to command {payload[0]:02x} {payload[1]:02x}")

    def _get(self, payload):
        reply_id = payload[0] + 1
        return self._transact(payload, lambda r: r[0] == reply_id and r[1] == GET)

    def _set(self, payload):
        ack = self._transact(
            payload, lambda r: r[0] == ACK and r[14] == payload[0] and r[15] == payload[1]
        )
        if ack[1] != ACK_OK:
            raise DeviceError(f"mic rejected command {payload[0]:02x} {payload[1]:02x} (status {ack[1]:#x})")

    def get_filters(self):
        """Return (high_pass, low_pass)."""
        bits = self._get([SETTINGS, GET])[7]
        return bool(bits & HIGH_PASS), bool(bits & LOW_PASS)

    def set_filters(self, high_pass=None, low_pass=None):
        """Set the filters; None keeps a filter's current state."""
        if high_pass is None or low_pass is None:
            hp, lp = self.get_filters()
            high_pass = hp if high_pass is None else high_pass
            low_pass = lp if low_pass is None else low_pass
        bits = (HIGH_PASS if high_pass else 0) | (LOW_PASS if low_pass else 0)
        # Same report NGENUITY sends: every other field zeroed, the mic ignores them.
        self._set([SETTINGS, SET, 0, 0, 0, bits])

    def get_presence_boost(self):
        """Return the presence boost in dB."""
        report = self._get([ONBOARD_EQ, GET, 0, 0, 0])
        return decode_gain(report[6], report[7])

    def set_presence_boost(self, db):
        db = round(min(max(db, BOOST_MIN), BOOST_MAX) / BOOST_STEP) * BOOST_STEP
        # save profile 1, eq type 0, eq index 0, then a single band (frequency/type ignored)
        self._set([ONBOARD_EQ, SET, 1, 0, 0, 0, *encode_gain(db), 0, 0, 0])


class Mixer:
    """Gain and mute through the standard USB Audio controls (ALSA)."""

    def __init__(self, card=None):
        self.card = find_alsa_card() if card is None else card
        self._range = None  # (lo, hi, db_lo, db_hi), fixed per card

    def _amixer(self, *args):
        try:
            out = subprocess.run(
                ["amixer", "-c", str(self.card), *args], check=True, capture_output=True, text=True
            )
        except FileNotFoundError:
            raise DeviceError("amixer not found (sudo apt install alsa-utils)") from None
        except subprocess.CalledProcessError as e:
            raise DeviceError(e.stderr.strip() or f"amixer {' '.join(args)} failed") from None
        return out.stdout

    def _read_volume(self):
        """Return the raw volume, caching the control's range on the first read."""
        out = self._amixer("cget", f"name={ALSA_VOLUME}")
        if self._range is None:
            lo, hi = map(int, re.search(r"min=(-?\d+),max=(-?\d+)", out).groups())
            db_lo, db_hi = map(float, re.search(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", out).groups())
            self._range = lo, hi, db_lo, db_hi
        return int(re.search(r": values=(-?\d+)", out).group(1))

    def _volume_range(self):
        if self._range is None:
            self._read_volume()
        return self._range

    def gain_range(self):
        _, _, db_lo, db_hi = self._volume_range()
        return db_lo, db_hi

    def get_gain(self):
        raw = self._read_volume()
        lo, hi, db_lo, db_hi = self._range
        return db_lo + (raw - lo) * (db_hi - db_lo) / (hi - lo)

    def set_gain(self, db):
        lo, hi, db_lo, db_hi = self._volume_range()
        raw = round(lo + (min(max(db, db_lo), db_hi) - db_lo) * (hi - lo) / (db_hi - db_lo))
        self._amixer("-q", "cset", f"name={ALSA_VOLUME}", f"{raw},{raw}")

    def get_muted(self):
        out = self._amixer("cget", f"name={ALSA_SWITCH}")
        return re.search(r": values=(\w+)", out).group(1) == "off"

    def set_muted(self, muted):
        self._amixer("-q", "cset", f"name={ALSA_SWITCH}", "off" if muted else "on")


def on_off(value):
    if value.lower() in ("on", "1", "true", "yes"):
        return True
    if value.lower() in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def main():
    parser = argparse.ArgumentParser(description="Configure a HyperX SoloCast 2.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status", help="show current settings (default)")
    p = sub.add_parser("filter", help="onboard frequency filters")
    p.add_argument("--high-pass", type=on_off, metavar="on|off")
    p.add_argument("--low-pass", type=on_off, metavar="on|off")
    p = sub.add_parser("boost", help=f"presence boost in dB ({BOOST_MIN:+g} to {BOOST_MAX:+g}, step {BOOST_STEP})")
    p.add_argument("db", type=float)
    p = sub.add_parser("gain", help="input gain in dB")
    p.add_argument("db", type=float)
    p = sub.add_parser("mute", help="mute the mic")
    p.add_argument("state", type=on_off, metavar="on|off")
    args = parser.parse_args()

    try:
        if args.cmd in (None, "status"):
            mixer = Mixer()
            with SoloCast2() as mic:
                hp, lp = mic.get_filters()
                boost = mic.get_presence_boost()
            print(f"gain           {mixer.get_gain():+.2f} dB")
            print(f"muted          {'yes' if mixer.get_muted() else 'no'}")
            print(f"high-pass      {'on' if hp else 'off'}")
            print(f"low-pass       {'on' if lp else 'off'}")
            print(f"presence boost {boost:+.1f} dB")
        elif args.cmd == "filter":
            with SoloCast2() as mic:
                mic.set_filters(args.high_pass, args.low_pass)
        elif args.cmd == "boost":
            with SoloCast2() as mic:
                mic.set_presence_boost(args.db)
        elif args.cmd == "gain":
            Mixer().set_gain(args.db)
        elif args.cmd == "mute":
            Mixer().set_muted(args.state)
    except DeviceError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
