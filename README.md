# SoloCast 2 control for Linux

Configure the HyperX SoloCast 2 (USB `03f0:0fbf`) without NGENUITY.

| Setting | Range | Via |
|---|---|---|
| Gain | −7.9 … +19.9 dB | ALSA |
| Mute | on / off | ALSA |
| High-pass / low-pass filter | on / off | vendor HID report `0x60` |
| Presence boost (3 kHz) | −6 … +6 dB, 0.5 dB steps | vendor HID report `0x66` |

NGENUITY's noise suppression and voice effects run on the host, not the mic,
so they aren't covered here. The HID protocol is documented at the top of
`solocast2.py`.

## Setup

Requires Python 3, `alsa-utils`, and for the GUI GTK 4 + libadwaita
(`gir1.2-adw-1`). Give your desktop session access to the mic's HID interface:

```sh
sudo cp udev/70-hyperx-solocast2.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=change --subsystem-match=hidraw
```

## Usage

```sh
./solocast2_gui.py                                   # GUI
./solocast2.py status                                # CLI
./solocast2.py filter --high-pass on --low-pass off
./solocast2.py boost 2
./solocast2.py gain 4
./solocast2.py mute off
```

Keep gain low (about +4 dB) and raise loudness downstream: mic gain is what clips.
