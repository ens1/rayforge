---
description: "Supported firmware in Rayforge — GRBL, Marlin, Smoothieware, and compatible controllers. Choose the right firmware for your laser cutter."
---

# Firmware Compatibility

This page documents firmware compatibility for laser controllers used with Rayforge.

## Overview

Rayforge is designed primarily for **GRBL-based controllers** but also supports Marlin, Smoothieware, and other firmware types.

### Compatibility Matrix

| Firmware         | Version       | Status        | Driver                           | Notes                  |
| ---------------- | ------------- | ------------- | -------------------------------- | ---------------------- |
| **GRBL**         | 1.1+          | Compatible    | GRBL Serial / GRBL Serial Simple | Recommended            |
| **grblHAL**      | 2023+         | Compatible    | GRBL Serial / GRBL Telnet        | Modern GRBL fork       |
| **GRBL**         | 0.9           | Limited       | GRBL Serial                      | Older, may have issues |
| **Smoothieware** | All           | Compatible    | SmoothieDriver (Telnet)          | Network-based          |
| **Marlin**       | 2.0+          | Compatible    | Marlin Serial                    | Laser mode required    |
| **ESP3D**        | All           | Compatible    | GRBL Telnet                      | Network-based          |
| **Ruida**        | 644XS profile | Experimental  | Ruida USB Serial / UDP Program   | Program transfer only  |
| **OctoPrint**    | All           | Experimental  | OctoPrint                        | See notes below        |
| **Other**        | -             | Not supported | -                                | Request support        |

---

## GRBL Firmware

**Status:** Fully Supported
**Versions:** 1.1+
**Drivers:** GRBL Serial, GRBL Serial Simple

### GRBL 1.1 (Recommended)

**What is GRBL 1.1?**

GRBL 1.1 is the most common firmware for hobby CNC and laser machines. Released in 2017, it's stable, well-documented, and widely supported.

**Features supported by Rayforge:**

- Serial communication (USB)
- Real-time status reporting
- Laser mode (M4 constant power)
- Settings read/write ($$, $X=value)
- Homing cycles ($H)
- Work coordinate systems (G54)
- Jogging commands ($J=)
- Feed rate override
- Soft limits
- Hard limits (endstops)

**Known limitations:**

- Power range: 0-1000 (S parameter)
- No network connectivity (USB only)
- Limited onboard memory (small G-code buffer)

### Checking GRBL Version

**Query version:**

Connect to your controller and send:

```
$I
```

**Response examples:**

```
[VER:1.1h.20190825:]
[OPT:V,15,128]
```

- `1.1h` = GRBL version 1.1h
- Date indicates build

### GRBL 0.9 (Older)

**Status:** Limited Support

GRBL 0.9 is an older version with some compatibility issues:

**Differences:**

- Different status report format
- No laser mode (M4) - uses M3 only
- Fewer settings
- Different jogging syntax

**If you have GRBL 0.9:**

1. **Upgrade to GRBL 1.1** if possible (recommended)
2. **Use M3 instead of M4** (less predictable power)
3. **Test thoroughly** - some features may not work

**Upgrade instructions:** See [GRBL Wiki](https://github.com/gnea/grbl/wiki)

### GRBL Serial Simple Driver

Rayforge includes a second GRBL serial driver for devices where the
standard buffer-counting driver causes false alarms or communication errors.

**How it works:**

- Uses a ping-pong protocol: send one line, wait for "ok", send the next
- No character-counting buffer management
- No deadlock detection or stall recovery
- Simpler and more predictable on some devices

**When to use:**

- Your device gets false buffer stall alarms with the standard driver
- Communication errors occur intermittently with the standard driver
- You have a device with unusual buffer behavior

**When not to use:**

- The standard GRBL Serial driver works reliably for most devices
- The simple driver lacks deadlock recovery, so jobs may stop on a lost
  "ok" response without automatic recovery

---

## grblHAL

**Status:** Compatible
**Versions:** 2023+
**Driver:** GRBL Serial

### What is grblHAL?

grblHAL is a modern fork of GRBL with enhanced features:

- Multiple controller hardware support (STM32, ESP32, etc.)
- Ethernet/WiFi networking
- SD card support
- More I/O pins
- Enhanced laser support

**Compatibility with Rayforge:**

- **Fully compatible** - grblHAL maintains GRBL 1.1 protocol
- All GRBL features work
- Additional features (networking, SD) not yet supported by Rayforge
- Status reporting identical to GRBL

**Using grblHAL:**

1. Select "GRBL Serial" driver in Rayforge
2. Connect via USB serial (just like GRBL)
3. All features work as documented for GRBL

**Future:** Rayforge may add support for grblHAL-specific features (networking, etc.)

---

## GRBL Telnet Driver

**Status:** Supported
**Firmware:** grblHAL, ESP3D, and other networked GRBL controllers
**Driver:** GRBL Telnet

### About the GRBL Telnet Driver

The GRBL Telnet driver connects to GRBL-based controllers over the network
via a Telnet interface. This is ideal for boards with built-in WiFi or
Ethernet — no USB cable required.

**Features:**

- Network connectivity (Ethernet/WiFi)
- Compatible with grblHAL and ESP3D-based boards
- Same GRBL protocol as the serial driver

**Using the GRBL Telnet driver:**

1. **Configure networking** on your controller (WiFi or Ethernet)
2. **Select "GRBL Telnet"** driver in machine settings
3. **Enter the IP address** and port of your controller
4. **Connect** — the driver communicates over Telnet

**Requirements:**

- Networked GRBL-compatible controller (grblHAL, ESP3D, etc.)
- Controller and computer on the same network
- Telnet interface enabled on the controller

---

## Smoothieware

**Versions:** All
**Driver:** SmoothieDriver (Telnet-based)

### About SmoothieDriver

Rayforge includes a dedicated SmoothieDriver that connects to Smoothieware controllers via Telnet over network. This provides native support rather than relying on GRBL compatibility mode.

**Features:**

- Network connectivity (Ethernet/WiFi)
- Real-time status reporting
- Native Smoothieware G-code support

**Using Smoothieware with Rayforge:**

1. **Configure network** on your Smoothieboard (Ethernet or WiFi)
2. **Select SmoothieDriver** in machine settings
3. **Enter IP address** of your controller
4. **Select Smoothieware dialect** in machine settings > G-code > Dialect

**Requirements:**

- Smoothieboard with network connectivity
- Controller and computer on same network
- Telnet enabled in Smoothieware config

**Limitations:**

- Requires network connection (no USB serial)
- Settings ($$ commands) work differently than GRBL

---

## Marlin

**Versions:** 2.0+ with laser support
**Driver:** Marlin Serial

### Marlin Serial Driver

Rayforge includes a dedicated MarlinSerialDriver that connects to Marlin firmware
via serial (USB). Marlin 2.0+ can control lasers when properly configured.

**Features:**

- Serial communication (USB)
- Marlin handshake protocol (waits for "start" message)
- G-code streaming line-by-line with `ok` acknowledgment
- M114 position polling
- Job execution with granular progress reporting
- Homing (G28), jogging, move-to, tool change (T)
- WCS offset setting (G10 L2 P)
- Laser power control via the Marlin G-code dialect
- Cancel via M410 (Quick Stop)
- Auto-configuration probing (queries M115, M211, M503)

**Requirements:**

1. **Marlin 2.0 or later** firmware
2. **Laser features enabled:**
   ```cpp
   #define LASER_FEATURE
   #define LASER_POWER_INLINE
   ```
3. **Correct power range** configured:
   ```cpp
   #define SPEED_POWER_MAX 1000
   ```

**Using Marlin with Rayforge:**

1. **Select "Marlin (Serial)"** driver in machine settings
2. **Set the serial port** and baud rate (typically 115200)
3. **Select Marlin dialect** in machine settings > G-code > Dialect
4. **Configure Marlin** for laser use
5. **Test power range** matches (0-1000 or 0-255)

**Limitations:**

- Experimental — feedback welcome
- Settings read/write (like GRBL's `$$`) not supported
- No network connectivity (USB only)

---

## Firmware Upgrade Guide

### Upgrading to GRBL 1.1

**Why upgrade?**

- Laser mode (M4) for constant power
- Better status reporting
- More reliable
- Better Rayforge support

**How to upgrade:**

1. **Identify your controller board:**
   - Arduino Nano/Uno (ATmega328P)
   - Arduino Mega (ATmega2560)
   - Custom board

2. **Download GRBL 1.1:**
   - [GRBL Releases](https://github.com/gnea/grbl/releases)
   - Get latest 1.1 version (1.1h recommended)

3. **Flash firmware:**

   **Using Arduino IDE:**

   ```
   1. Install Arduino IDE
   2. Open GRBL sketch (grbl.ino)
   3. Select correct board and port
   4. Upload
   ```

   **Using avrdude:**

   ```bash
   avrdude -c arduino -p m328p -P /dev/ttyUSB0 \
           -U flash:w:grbl.hex:i
   ```

4. **Configure GRBL:**
   - Connect via serial
   - Send `$$` to view settings
   - Configure for your machine

### Backup Before Upgrade

**Save your settings:**

1. Connect to controller
2. Send `$$` command
3. Copy all settings output
4. Save to file

**After upgrade:**

- Restore settings one-by-one: `$0=10`, `$1=25`, etc.
- Or use defaults and reconfigure

---

## Controller Hardware

### Common Controllers

| Board                  | Typical Firmware | Rayforge Support |
| ---------------------- | ---------------- | ---------------- |
| **Arduino CNC Shield** | GRBL 1.1         | Excellent        |
| **MKS DLC32**          | grblHAL          | Excellent        |
| **Ruida**              | Proprietary      | Experimental     |
| **OctoPrint (Pi)**     | Various          | Experimental     |

### Recommended Controllers

For best Rayforge compatibility:

1. **Arduino Nano + CNC Shield** (GRBL 1.1)
   - Cheap (~$10-20)
   - Easy to flash
   - Well documented

2. **MKS DLC32** (grblHAL)
   - Modern (ESP32-based)
   - WiFi capable
   - Active development

3. **Custom GRBL boards**
   - Many available on marketplaces
   - Check for GRBL 1.1+ support

---

## Firmware Configuration

### GRBL Settings for Laser

**Essential settings:**

```
$30=1000    ; Max spindle/laser power (1000 = 100%)
$31=0       ; Min spindle/laser power
$32=1       ; Laser mode enabled (1 = on)
```

**Machine settings:**

```
$100=80     ; X steps/mm (calibrate for your machine)
$101=80     ; Y steps/mm
$110=3000   ; X max rate (mm/min)
$111=3000   ; Y max rate
$120=100    ; X acceleration (mm/sec)
$121=100    ; Y acceleration
$130=300    ; X max travel (mm)
$131=200    ; Y max travel (mm)
```

**Safety settings:**

```
$20=1       ; Soft limits enabled
$21=1       ; Hard limits enabled (if you have endstops)
$22=1       ; Homing enabled
```

### Testing Firmware

**Basic test sequence:**

1. **Connection test:**

   ```
   Send: ?
   Expect: &lt;Idle|...&gt;
   ```

2. **Version check:**

   ```
   Send: $I
   Expect: [VER:1.1...]
   ```

3. **Settings check:**

   ```
   Send: $$
   Expect: $0=..., $1=..., etc.
   ```

4. **Movement test:**

   ```
   Send: G91 G0 X10
   Expect: Machine moves 10mm in X
   ```

5. **Laser test (very low power):**
   ```
   Send: M4 S10
   Expect: Laser turns on (dim)
   Send: M5
   Expect: Laser turns off
   ```

---

## Troubleshooting Firmware Issues

### Firmware Not Responding

**Symptoms:**

- No response to commands
- Connection fails
- Status not reported

**Diagnosis:**

1. **Check baud rate:**
   - GRBL 1.1 default: 115200
   - GRBL 0.9: 9600
   - Try both

2. **Check USB cable:**
   - Data cable, not charge-only
   - Replace with known-good cable

3. **Check port:**
   - Linux: `/dev/ttyUSB0` or `/dev/ttyACM0`
   - Windows: COM3, COM4, etc.
   - Correct port selected in Rayforge

4. **Test with terminal:**
   - Use screen, minicom, or PuTTY
   - Send `?` and see if you get response

---

## Additional Controller Support

### Ruida Controllers

Rayforge includes an experimental, transfer-only backend for Ruida
controllers. It generates complete Ruida `.rd` programs and can transfer them
through either **Ruida (USB Serial)** or **Ruida (UDP Program)**. The protocol
compiler is provided by
[ruida-re](https://github.com/ens1/ruida-re), whose current execution evidence
profile is based on LightBurn 2.1.03 output for a Ruida 644XS controller.

:::warning
The advanced profiles described below remain evidence-limited and intended
for research. Narrow `planned-path-research` coupons now have single-section
and two-section cross-hatch observations. Two `dynamic-power-research` coupons
exposed a missing baseline restoration in the executed payloads; exact single-
and repeated-restore sequences are now operator-observed. Neither result
promotes the broad profiles. A nominal-0% marking control unexpectedly emitted
on the tested Boss LS2040, so zero encoded power must not be treated as a
laser-off control. Dwell, Z, and the other advanced profiles remain
hardware-unobserved.
Keep the `proven` profile selected unless you are intentionally evaluating one
narrow research capability.
:::

Before a Rayforge release can depend on this integration, `ruida-re` 0.1.0
must be published. The Git revision used by development builds does not satisfy
Rayforge's public `ruida-re==0.1.0` package dependency.

#### Program generation boundary

Rayforge remains responsible for image processing, path planning, overscan,
coordinate transforms, and other toolpath preparation. The final
machine-space `Ops` stream carries explicit process boundaries using
`ProcessStart` version 1 metadata. `RuidaEncoder` translates that neutral
stream into a `ruida-re` `JobPlan`; `ruida-re` then compiles the plan into a
complete, checksummed `.rd` program.

Raster axis and angle metadata retain the source-space planning intent after
workpiece transforms. For the proven profile, the Ruida backend derives the
effective horizontal or vertical axis from micrometer-quantized machine-space
scanlines. Rotations and reflections are therefore preserved. Constant-power
diagonal and cross-hatch motion uses a separate planned-path research profile;
the default profile continues to reject it.

This boundary keeps Ruida protocol details out of Rayforge's geometry and
image pipeline while allowing another laser application to integrate the same
`ruida-re` planning and protocol library.

#### Proven default profile

- Flat XY vector cutting and engraving
- Mixed vector and raster layers
- Horizontal and vertical raster scans
- Cardinal machine-space scans produced by rotated or reflected source work
- Unidirectional and bidirectional raster strategies
- Grayscale power modulation
- One laser head and explicit air-assist state

This conservative `proven` profile is selected by default and remains the only
profile intended for non-research use. It rejects all advanced capabilities
below.

##### Native variable-power raster observation

One exact Rayforge-generated horizontal native-raster row was transferred once
to the operator-identified Boss LS2040 over USB serial at 100 mm/s. It used a
requested 5%-15% layer range, paired normalized `C7`/`C2` modulation, and only
`AA` marking chunks no longer than 4 mm. The decoded plan contains a 23 mm
mark, an 11 mm semantic travel gap, and a 26 mm mark. The largest modeled
effective output is approximately 14.899%. The host reported one packet and
zero retries, with no controller or execution acknowledgement. The operator
reported, "Everything is as expected".

The exact artifact and scoped report are retained in the
[`variable-raster` evidence manifest](../../../tests/machine/driver/ruida/fixtures/hardware/boss-ls2040-usb-serial-rayforge-variable-raster-v1/manifest-v1.json).
The decoded lengths and output percentages are not measurements. This result
does not establish calibrated power, distinguish the positive modulation
levels optically, prove zero output in the gap, validate another row or scan
mode, or promote broader compatibility.

#### Evidence-limited research profiles

Each profile must be selected explicitly in the Ruida driver settings. Every
research profile emits an encoder warning, accepts exactly one layer within
its narrow scope, and fails closed before controller I/O when the job exceeds
that scope. The profiles cannot be combined.

| Profile | Narrow accepted scope |
| :------ | :-------------------- |
| `planned-path-research` | One planned-path raster layer using constant binary power for diagonal or cross-hatch scans. Variable-power, grayscale, and depth-map diagonal scans remain unsupported. |
| `dual-laser-research` | One vector layer using either controller channel 1 or channel 2. The inactive channel's stored powers must be entered and explicitly confirmed. Simultaneous channel mask 3 is not supported. |
| `stationary-research` | One vector layer containing Dwell events greater than 0 and no longer than 200 ms. Rayforge currently produces these only through manual Ops or frame corner pauses. This is not stationary marking Pulse. Exact 100 ms C6 11 delays after travel are operator-observed in one- and four-delay coupons; mark-adjacent and 200 ms dwell remain unobserved, the broad profile remains research-only, and static 0% marking is rejected. |
| `rf-research` | One vector layer with RF frequency from 10,000 through 20,000 Hz. |
| `fiber-research` | One vector layer on a fiber head with pulse width from 0 through 0.2 µs, encoded as 0 through 200 ns. |
| `z-research` | One native raster layer with a nonzero typed logical layer Z offset no greater than 1 mm in either direction. The compiler emits a balanced relative envelope and still requires all motion endpoints at Z=0. The prepared Z coupons were withheld and remain hardware-unobserved. |
| `dynamic-power-research` | One vector layer with Rayforge head-1 intent whose tab transform produces reduced positive marking power. A normal mark after a reduced span requires an explicit baseline-power restoration from a restoration-capable compiler. Exact one-restore and two-restore subsets are operator-observed; the broad profile remains research-only and other combinations are unvalidated. It does not provide general speed-dependent or raster dynamic power. |

The planned-path observations used the same operator-identified Boss LS2040
and USB serial transport. A direct 10% coupon produced the expected five
movements without visible marks. A direct 15% coupon produced five visible
lines, and a separate 15% job generated end to end through Rayforge also
produced five visible lines. Each used one `RasterSection` and ran at
100 mm/s.

The exact Rayforge observation was one layer with five alternating-direction
marking events. Its Rayforge scan angle was configured to 45 degrees. The job
used head-1 intent, 14.9972532503% encoded power, and an air-assist request of
off. The Rayforge machine model used a top-right origin. The reviewed 538-byte
program was transferred in one reported packet with zero reported retries.
The transport provided no controller or execution acknowledgement; the
operator observed five lines, more widely spaced and in the opposite diagonal
direction from the direct reference job.

The separate cross-hatch artifact contained two `RasterSection` blocks, five
marks in each diagonal direction, and one section-separator operation. At 15%
requested power and 100 mm/s, the operator reported, "Crosshatch is good. Both
directions are visible, no connection burns, and no burns. I can see the one
small edge, the beam obviously pulsed at the top left of the crosshatch." The
small edge decodes as a final 0.3507 mm `cut_relative` marking command. The
payload has no C6 10 record, so this observation is not evidence for pulse
control. Its host summary reported one packet and zero retries, with no
controller or execution acknowledgement.

These planned-path observations do not provide dimensional, angle, or power
metrology. They also do not validate other planned paths, additional layers,
variable-power, grayscale, or depth-map diagonal scans, other power or speed
settings, laser-channel routing or additional heads, UDP transfer, or
controller status monitoring. The physical air-assist state was not
independently observed.

##### Dynamic-vector power observations

Four Rayforge-generated, one-layer vector coupons with Rayforge head-1 intent
were transferred to the same Boss LS2040 over USB serial at 100 mm/s. The first
contained planned spans of 12 mm at 15%, 6 mm at 10%, and 12 mm at 15%. The
operator reported,
"It looks pretty solid. Maybe go longer and vary more." That observation did
not distinguish the middle span and did not establish that power changed or
returned to its baseline value.

The second contained three planned 30 mm spans at 15%, 5%, and 15%. The
operator reported good motion, that the first 30 mm was good, and "only the
first 30mm." The reviewed 539-byte payload set reduced active power immediately
before the middle span but contained no baseline-power restore before the
trailing ordinary mark. The observation is consistent with that reduced state
remaining active, leaving both later spans below the cardboard's visible
marking threshold. It does not establish zero optical output or calibrated
power at any percentage.

Rayforge now requires a `ruida-re` compiler that advertises dynamic restoration
contract 1. That compiler emits an explicit layer-baseline power envelope
before an ordinary baseline mark that follows a reduced-power mark. Rayforge
rejects the dynamic job before controller I/O when that contract is absent.

The third coupon used the same planned 30 mm spans at 15%, 5%, and 15%, with
the corrected explicit restore envelope before the trailing mark. The operator
reported, "Perfect. A ~30mm line, a gap, and a ~30mm line." This is
operator-observed evidence for that exact restore subset on one machine. The
approximate visual report is not dimensional metrology and does not establish
calibrated power or zero optical output in the gap.

The fourth coupon contained five planned 16 mm spans at
15%-5%-15%-5%-15%, with reduce-restore-reduce-restore envelopes immediately
before the last four spans. The operator reported, "Yes, I see 3 lines, maybe
20mm each, two gaps." The reported lengths are approximate visual descriptions,
not metrology; the decoded spans are 16 mm. This is scoped evidence for the
exact repeated-restore sequence, not arbitrary repeated dynamic behavior.

All four host-side transfer summaries reported one packet and zero retries.
They provided no controller acknowledgement or execution-completion status.
The observations provide no dimensional, timing, optical-power, electrical,
or laser-channel-routing metrology. The broad dynamic-power profile remains
research-only and is not eligible for default promotion. Other powers, speeds,
geometry, repeated patterns, additional heads, controllers, transports, or
combinations with another research capability remain unvalidated.

##### Travel-only stationary dwell observation

A staged Rayforge-generated set compared a no-dwell control with one and four
exact 100 ms `C611` `additional_delay` records on the same Boss LS2040. All
three files contained one planned 5 mm anchor at 15% requested power and
100 mm/s, followed only by four absolute travel moves. The sentinel placed one
delay after the first travel; the full coupon placed a delay after every
travel. The files contained no `C610` pulse and no marking command after the
anchor.

Each exact artifact was transferred once after separate operator approval.
Each host summary reported one packet and zero retries, with no controller or
execution acknowledgement. For the control, the operator reported, "I see one
faint line, vertical, about 5mm". For the one-delay sentinel, the report was,
"It looks like it did a rectangle with pauses at the corner? Nothing other
than a horizontal line, about 5mm". For the four-delay coupon, the operator
reported, "Yes, one faint line, pauses at the corners".

This establishes only operator-observed pause behavior for those exact
travel-then-100 ms delay artifacts, without visible post-anchor marking in the
reports. No timing or motion instrumentation measured the pauses, and the
approximate line descriptions are not dimensional or orientation metrology.
The result does not validate mark-adjacent dwell, 200 ms dwell, stationary
marking Pulse, arbitrary sequences, or the broad `stationary-research`
profile.

##### Zero-power safety observation

Before a C6 11 dwell test, a paired no-dwell control was generated with zero
layer and active power for laser 1, an enabled laser-1 mask, and four ordinary
cut motions around decoded 45 by 17 mm bounds. The operator reported, "There
was laser emission. I see a clearly drawn rectangle, maybe 25mmx50mm." The
operator dimensions are approximate and orientation-dependent, but visible
emission directly contradicts treating raw zero power as a laser-off safety
control on this machine.

The exact cause was not isolated. Raw zero could mean default, stale, or no
update; the controller or power supply could impose a firing floor; and other
fields, including the minimal through-power records, may contribute. None of
those explanations is established. The paired dwell artifact differs only by
four 200 ms C6 11 records and its checksum. It was stopped before transfer,
published only with an `.rd.quarantined` suffix, and remains do-not-send. The Z
coupons were also withheld. Consequently that 200 ms marking-path dwell pair
and nonzero logical Z behavior remain hardware-unobserved. The later scoped
travel-only 100 ms observation above does not rehabilitate or supersede the
quarantined pair.

Rayforge rejects static zero-power marking motion. Its `ruida-re` compiler also
rejects every enabled marking-channel minimum and maximum, and raster marking
modulation, that would encode below raw value 16. This is a conservative
generation-time evidence floor, not a guarantee that raw 16 or any low power
is safe on a particular laser. It applies only when compiling a plan; existing,
cached, hand-authored, or externally supplied `.rd` files are not rewritten or
made safe retroactively.

The following remain unsupported for every profile:

- Rotary motion and rotary attachments
- Cut-through start/end controls
- Generic endpoint Z motion, Z-per-pass, and arbitrary 2.5D motion
- Stationary marking Pulse
- Simultaneous firing of both Ruida laser channels
- Combining research capabilities, or using them in multi-layer jobs

These requests are rejected instead of being approximated with
controller-specific guesses.

#### Transfer behavior

- **Ruida (USB Serial)** opens the controller link without a reply probe and
  transfers the complete program over USB serial.
- **Ruida (UDP Program)** probes the controller link, then transfers the same
  complete program over UDP.
- A successful transfer confirms that the program bytes were delivered under
  the transport's protocol contract. It does **not** confirm that physical
  execution has completed.
- After a completed or ambiguous transfer, Rayforge treats controller
  execution as unconfirmed and refuses another transfer. Reconnect only after
  the controller is visibly idle.

:::warning
The transfer-only drivers do not implement Ruida device management, position
or execution status, homing, jogging, hold/resume, cancel, controller settings,
or immediate laser controls. Use the machine's physical controller panel for
those operations.
:::

Ruida is a binary protocol, so the G-code console, G-code macros, and G-code
device settings do not apply to these drivers.

---

### OctoPrint

Rayforge includes an experimental OctoPrint driver that submits G-code directly
to an OctoPrint server over the network. This is useful if your laser is
connected to a Raspberry Pi or other machine running OctoPrint.

**Features:**

- WebSocket connection for real-time status updates
- REST polling fallback when WebSocket is unavailable
- Auto-reconnect on connection loss
- Job submission with automatic print start
- Jogging, homing, and pause/resume controls
- "Request Access" flow for OctoPrint application keys

**Using the OctoPrint driver:**

1. Select "OctoPrint" driver in machine settings
2. Enter the hostname or IP address of your OctoPrint server
3. Set the port (default: 80)
4. Click "Request Access" to obtain an API key through OctoPrint's
   application key flow
5. Connect -- Rayforge will establish a WebSocket connection

**Limitations:**

- Experimental and untested on real hardware -- feedback welcome
- Cannot read or write firmware settings through OctoPrint
- Probe results are not reported by OctoPrint
- WCS offset reads are not supported

---

### Contributing

To add firmware support:

1. Implement driver in `rayforge/machine/driver/`
2. Define G-code dialect in `rayforge/machine/models/dialect.py`
3. Test thoroughly on real hardware
4. Submit pull request with documentation

---

## Related Pages

- [G-code Dialects](gcode-dialects) - Dialect details
- [Device Settings](../machine/device.md) - GRBL configuration
- [Connection Issues](../troubleshooting/connection.md) - Connection troubleshooting
- [General Settings](../machine/general.md) - Machine setup
