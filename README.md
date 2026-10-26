# LED Strip

A cheap 12 V RGB strip controller (the kind sold with a 24-key IR remote), rebuilt around an
**ESP32-C3 Super Mini** and controlled from Windows over **Bluetooth LE** by a small tray app.

- **Manual mode:** solid colors and looping color effects (Rainbow, Breathe, …), with your
  own presets and a Photoshop-style gradient editor whose end loops back to its start.
- **Screen sync:** the strip follows the colors on your screen.
- **Follows the PC:** the strip is lit only while the app is connected. It fades off when the
  laptop sleeps, shuts down or leaves range, or when the app quits, and comes back by itself.
- No pairing, no Wi-Fi, no cloud. A USB cable works as a wired alternative.

| File | What it is |
|---|---|
| `ledstrip_ble.ino` | ESP32-C3 firmware (Arduino) |
| `led_screen_sync.pyw` | Windows tray app (Python) |

---

## Hardware

### Parts

- ESP32-C3 Super Mini
- The original controller's board, reused as a MOSFET driver (marked `JSY-LP001`, three SOT-23
  MOSFETs, one per color)
- 12 V adapter (the one that came with the strip)
- 12 V → 5 V converter for the ESP: a car USB charger works well (it's a buck converter in a shell)
- Recommended: three 10 kΩ resistors

### Wiring

Remove the controller's 8-pin chip (or cut its output pins) so it can't fight the ESP over the
MOSFET gates.

| From | To |
|---|---|
| ESP **GPIO 5** | red MOSFET gate |
| ESP **GPIO 6** | green MOSFET gate |
| ESP **GPIO 7** | blue MOSFET gate |
| ESP **GND** | board GND (the 12 V input's **−** pad) |
| 12 V adapter | board's 12 V input pads, and the strip's common **+** (black wire) |
| Car charger input | 12 V input (centre tip **+**, side springs **−**) |
| Car charger USB output | ESP USB-C port (power) |

- The strip is **common anode**: its black wire is +12 V. Each MOSFET connects one color to
  ground. **Never connect anything from the ESP to the black wire.**
- **Recommended:** a 10 kΩ resistor from each gate to GND. Without these, the gates float for a
  moment while the ESP boots or resets, and the strip can flash.
- Power the ESP from **one** source only: the charger *or* a USB cable from the PC, not both.

### Compatible strips

Plug and play for most **12 V, 4-pin, common-anode RGB strips** (`+12V R G B`), up to roughly
**5 m of standard 60 LED/m strip** (about 2 A per color). Check that the strip's **+** lands on
the black (+12 V) pin. If two colors come out swapped, change `PIN_R` / `PIN_G` / `PIN_B` in the
firmware.

Not supported without changes: 24 V strips (the board is rated 5–12 V), common-cathode strips,
the white channel of RGBW strips, and addressable strips (WS2812, WS2815, …).

---

## Firmware (`ledstrip_ble.ino`)

Arduino IDE with the **Arduino-ESP32 core 3.x**. It uses only the built-in BLE library.

1. Arduino needs the sketch inside a folder of the same name. Open the file and let the IDE
   create the `ledstrip_ble` folder, or put it there yourself.
2. **Tools** settings:
   - Board: **ESP32C3 Dev Module**
   - USB CDC On Boot: **Enabled** (needed for the USB connection)
   - CPU Frequency: **80 MHz (WiFi)**. That's the lowest the radio allows, and plenty for this.
3. **Quit the tray app before uploading.** It holds the USB port while connected.

At power-up the strip stays dark and the board advertises over Bluetooth as **"LED Strip"**.

---

## Tray app (`led_screen_sync.pyw`)

### Install

Python 3.10 or newer with **tcl/tk** (tick *tcl/tk and IDLE* in the Python installer; it can't be
installed with pip). Then:

```
py -m pip install pystray pillow mss numpy bleak pyserial
```

The package is **pyserial**. One called just `serial` is unrelated and won't work.

### Run

```
pyw led_screen_sync.pyw
```

On first run it adds itself to Windows startup. Toggle *Start with Windows* in the right-click
menu. The startup entry points at this file's location, so keep the file where it is.

Bluetooth must be on. The app finds the strip by itself and remembers it, so later reconnects are
faster. If the board is plugged into the PC by USB, the cable is used instead.

### Using it

- **Left-click** the tray icon to open the panel: power switch, *Manual* / *Screen sync* tabs,
  and a status line.
- **Right-click** for the quick menu: Light on, Screen sync, Presets, Start with Windows, Quit.
- The tray icon shows the strip's current color, or grey when it's off or not connected.

**Manual mode**
- Click a preset to show it. Solid colors are dots; color loops are little color wheels.
- *Custom color…* picks any one-off color. *Brightness* applies to everything in this mode.
- **Double-click** a preset to edit it. **Right-click** to edit, duplicate or delete it.
- The **+** tile (or *New preset…* in the tray menu) creates a new preset.

**Preset editor**
- *Solid color* or *Color loop*.
- **Color loops:** click the bar to add a color, drag a marker to move it, double-click a marker
  to recolor it, drag a marker down off the bar to remove it (a loop needs at least two colors).
- The end of the bar blends back into the start, so the loop never jumps.
- **Loop time:** 0.5 s to 5 minutes. **Blend:** *Smooth* glides between colors, *Steps* jumps.
- Changes preview live on the strip. *Cancel* restores what was showing; *Save as new* keeps the
  original preset.

**Screen sync**

| Setting | Effect |
|---|---|
| Brightness | multiplier; above 1 lifts dark scenes (capped at full) |
| Color boost | saturation; 1 = true to the screen |
| Responsiveness | how quickly it follows the screen (lower = calmer) |
| Strip fade | how long the strip glides between updates |
| Update rate | updates per second |
| Monitor | which screen to follow |

Exclusive-fullscreen games may capture as black. Run them in borderless windowed mode.

### Files it keeps

`%APPDATA%\LEDScreenSync\`
- `settings.json`: settings and presets (back this up to keep your presets)
- `log.txt`: errors and connection events

---

## How it works

### Bluetooth LE protocol

The strip advertises service `6e4c0001-7a3b-4c8e-9f21-3d5b8a6c0e11` (name **LED Strip** in the
scan response). No pairing.

| Characteristic | UUID | Properties | Purpose |
|---|---|---|---|
| Control | `6e4c0002-7a3b-4c8e-9f21-3d5b8a6c0e11` | Write, Write Without Response | everything the app sends |
| State | `6e4c0003-7a3b-4c8e-9f21-3d5b8a6c0e11` | Read | JSON status, used to verify the device |

**Control packet:** 10 bytes, little-endian.

| Bytes | Type | Meaning |
|---|---|---|
| 0–1 | u16 | red × 256 (0–65280 = 0.0–255.0) |
| 2–3 | u16 | green × 256 |
| 4–5 | u16 | blue × 256 |
| 6 | u8 | brightness 0–255 |
| 7 | u8 | flags, bit 0 = on |
| 8–9 | u16 | fade time in ms (max 10000) |

Example, warm white `#FFB070` at brightness 200, on, 250 ms fade:

```
00 FF  00 B0  00 70  C8  01  FA 00
```

Colors are scaled by 256 so screen sync can send fractional values, which keeps dark scenes
smooth. Ten bytes fit Bluetooth's default 20-byte payload, so the MTU doesn't matter.

**State** (read): `{"device":"ledstrip","on":true,"r":255,"g":176,"b":112,"brightness":200,"fade":250,"client":true}`

### Keep-alive and off rules

- The app writes on every change, and re-sends at least every **1.5 s**.
- If nothing arrives for **6 s**, the strip fades off over 800 ms (app frozen, PC hung).
- If the Bluetooth connection drops, it fades off **immediately** and starts advertising again.
- When the app quits, it sends one last *off* packet, then disconnects.

The firmware only overrides its target when turning off. The stored color is kept, so the next
packet brings everything back.

### USB serial protocol

One command per line. Parameters are URL-encoded like a query string.

| Command | Reply |
|---|---|
| `state` | state JSON |
| `set r=255&g=0&b=80&brightness=200&on=1&fade=250` (or `hex=ff0050`) | applies, replies with state JSON |
| `put …` | same as `set`, no reply (for streaming) |

`put` and `set` count as keep-alive, exactly like Bluetooth writes.

### Firmware output

About 1000 times a second the firmware moves the current color toward the target. The fade is
exponential and happens in perceptual space (about 95 % done after the fade time). It then
applies gamma 2.2 and drives 12-bit PWM at 5 kHz on GPIO 5/6/7. Dithering alternates between
neighbouring PWM steps, so dim levels land between steps instead of jumping.

### Effects

A color loop is a list of stops on a ring (`pos` 0–1). The app travels around it once every
`period` seconds, sending about 30 updates a second, and the strip's fade smooths the steps in
between. Presets in `settings.json` look like this:

```json
{"id": "a1b2c3d4e5", "name": "Warm white", "type": "solid", "color": "#ffb070"}

{"id": "f6e7d8c9b0", "name": "Rainbow", "type": "loop", "period": 12, "blend": "smooth",
 "stops": [{"pos": 0, "color": "#ff0000"}, {"pos": 0.1667, "color": "#ffff00"}, "…"]}
```

`blend` is `"smooth"` or `"steps"`.

### Screen capture

On Windows the app captures with GDI `StretchBlt` (`HALFTONE`, plain `SRCCOPY`), shrinking the
screen to 160 px wide in one step. It doesn't use mss's capture, because mss adds `CAPTUREBLT`,
which makes Windows redraw the mouse cursor on every frame (visible flicker in some apps). mss
is still used to list monitors.

The color picked from a frame favours bright, saturated pixels over grey and black, and the strip's
brightness follows the screen's.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| *Strip not found over Bluetooth* | Check Windows Bluetooth is on and the strip has power. It reconnects by itself. |
| *COMx is in use* | Close the Arduino Serial Monitor. |
| Upload fails | Quit the tray app first. If it still fails: hold BOOT, tap RESET, release BOOT, upload. |
| Strip flashes at power-up | Add the 10 kΩ gate pulldowns. |
| Two colors swapped | Change `PIN_R` / `PIN_G` / `PIN_B` in the firmware. |
| `No module named 'tkinter'` | Re-run the Python installer → Modify → tick *tcl/tk and IDLE*. |
| Tray icon not visible | Windows 11 hides new icons under the **^** arrow. Drag it onto the taskbar. |
| Anything else | See `%APPDATA%\LEDScreenSync\log.txt`. |
