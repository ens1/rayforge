[![GitHub Release](https://img.shields.io/github/release/barebaric/rayforge.svg?style=flat)](https://github.com/barebaric/rayforge/releases/)
[![PyPI version](https://img.shields.io/pypi/v/rayforge)](https://pypi.org/project/rayforge/)
[![Snap Release](https://snapcraft.io/rayforge/badge.svg)](https://snapcraft.io/rayforge)
[![Launchpad PPA](https://img.shields.io/badge/PPA-blue)](https://launchpad.net/~knipknap/+archive/ubuntu/rayforge)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[![Get it from the Snap Store](https://snapcraft.io/en/light/install.svg)](https://snapcraft.io/rayforge)
<a href="https://flathub.org/apps/org.rayforge.rayforge"><img alt="Get it from Flathub" src="website/static/images/flathub-badge.svg" height="55"/></a>
<a href="https://www.patreon.com/c/knipknap"><img src="https://c5.patreon.com/external/logo/become_a_patron_button.png" alt="Become a Patron" height="55"/></a>

# Rayforge

Rayforge is a modern, cross-platform 2D CAD, G-code sender and control
software for GRBL, Marlin, and Smoothieware-based laser cutters and
engravers. It also generates and transfers programs for Ruida controllers.
Built with Gtk4 and Libadwaita, it provides a clean, native interface for Linux, MacOS and Windows, offering a full suite of tools
for both hobbyists and professionals.

![Screenshot](website/static/screenshots/main-3d-rotary.png)

You can also check the [official Rayforge homepage](https://rayforge.org).
We also have a [Discord](https://discord.gg/sTHNdTtpQJ).

## Key Features

### Design & Editing

| Feature                      | Description                                                                             |
| :--------------------------- | :-------------------------------------------------------------------------------------- |
| **Parametric Sketch Editor** | Create precise, constraint-based 2D designs with geometric and dimensional constraints. |
| **Comprehensive 2D Canvas**  | Full suite of tools: alignment, transformation, measurement, zoom, pan, and more.       |
| **Multi-Layer Operations**   | Assign different operations (e.g., engrave then cut) to layers in your design.          |
| **Stock Material System**    | Document-level stock with geometry, thickness, and material assignment.                 |
| **Undo/Redo**                | Full undo/redo support across all document operations.                                  |
| **Broad File Support**       | Import from SVG, DXF, PDF, JPEG, PNG, BMP, and Ruida (`.rd`). Export to SVG and DXF.    |
| **Project Files (.ryp)**     | Compressed project format preserving all assets, layers, and configurations.            |

### Operations & Toolpaths

| Feature                      | Description                                                                                          |
| :--------------------------- | :--------------------------------------------------------------------------------------------------- |
| **Versatile Operations**     | Supports Contour, Raster Engraving (with cross-hatch fill), Shrink Wrap, Depth Engraving, and Frame. |
| **2.5D Cutting**             | Multi-pass cuts with configurable step-down for thick materials.                                     |
| **True 4th Axis Support**    | Full rotary axis support - as 4th axis, or axis replacement mode for hobby machines.                 |
| **Animated 3D Simulation**   | Simulate toolpaths in 3D with animated playback, scrubber, and speed control.                        |
| **Holding Tabs**             | Add tabs to contour cuts. Supports manual and automatic placement.                                   |
| **Overscan & Kerf Comp.**    | Improve engraving quality with overscan; ensure dimensional accuracy with kerf compensation.         |
| **Dithering Algorithms**     | Floyd-Steinberg and Bayer ordered dithering for high-quality raster engraving.                       |
| **Post-Processors**          | Lead-in/lead-out, merge overlapping lines, and crop toolpaths to stock boundary.                     |
| **Advanced Path Generation** | Image tracing, travel time optimization, path smoothing, and spot size interpolation.                |

### Machine Control

| Feature                         | Description                                                                                    |
| :------------------------------ | :--------------------------------------------------------------------------------------------- |
| **Multi-Machine Profiles**      | Configure and instantly switch between multiple machine profiles.                              |
| **Device Profiles**             | Declarative device packages with import/export for sharing configurations.                     |
| **Work Coordinate Systems**     | 6 WCS (G54-G59) with per-layer assignment for cutting at different offsets.                    |
| **No-Go Zones**                 | Define restricted areas with collision detection before sending G-code.                        |
| **Machine Hours & Maintenance** | Track operating hours with configurable maintenance counters and notification thresholds.      |
| **GRBL Firmware Settings**      | Read and write firmware parameters (`$$`) directly from the UI.                                |
| **Arc & Bezier Curves**         | Native G2/G3 arc and G5 bezier curve support with automatic linearization.                     |
| **Multi-Laser Operations**      | Choose different lasers for each operation in a job.                                           |
| **G-code Dialects**             | Supports GRBL, Smoothieware, Marlin, LinuxCNC, Mach4, and custom dialects via built-in editor. |
| **G-code Macros & Hooks**       | Run custom G-code snippets before/after jobs. Supports variable substitution.                  |
| **Pre-flight Checks**           | Validates bounds, work area, and no-go zone collisions before sending a job.                   |
| **G-code Console**              | Interactive console with syntax highlighting and search.                                       |

### Materials & Presets

| Feature                  | Description                                                                                     |
| :----------------------- | :---------------------------------------------------------------------------------------------- |
| **Material Library**     | 60+ built-in materials across categories with search and user-created material libraries.       |
| **Recipe/Preset System** | Auto-matching presets by material, thickness, machine, and laser head with specificity scoring. |
| **Material Test Grid**   | Generate power/speed test grids to find optimal laser settings for a given material.            |

### Workflow & Automation

| Feature                     | Description                                                                                   |
| :-------------------------- | :-------------------------------------------------------------------------------------------- |
| **Camera Integration**      | USB camera for workpiece alignment, positioning, background tracing, and fisheye calibration. |
| **AI Workpiece Generation** | Generate SVG workpieces from text prompts using OpenAI-compatible AI providers.               |
| **Print & Cut Alignment**   | Align cuts to printed material using registration marks with a guided wizard.                 |
| **Headless/CLI Mode**       | Worker-only mode without UI for batch processing and automation.                              |
| **Projector Mode**          | Project toolpaths onto your machine bed for alignment.                                        |

### Platform & Extensibility

| Feature            | Description                                                                                              |
| :----------------- | :------------------------------------------------------------------------------------------------------- |
| **Modern UI**      | Polished UI built with Gtk4 and Libadwaita. Supports system, light, and dark themes.                     |
| **Addon System**   | Built-in addon manager for installing and managing community extensions.                                 |
| **Extensible**     | Open development model makes it easy to [add support for new devices](website/docs/developer/driver.md). |
| **Cross-Platform** | Native builds for Linux, Mac and Windows.                                                                |
| **Multi-Language** | Available in English, Portuguese, Spanish, German, French, Ukrainian, and Chinese.                       |
| **Update Checker** | Automatic background check for new versions via the GitHub Releases API.                                 |

### Device Support

| Device Type      | Connection Method       | Notes                                                                    |
| :--------------- | :---------------------- | :----------------------------------------------------------------------- |
| **GRBL**         | Serial Port             | Supported since version 0.13. The most common connection type.           |
| **GRBL**         | Telnet                  | Supported since version 0.16.                                            |
| **GRBL**         | Network (WiFi/Ethernet) | Connect to any GRBL device on your network.                              |
| **Smoothieware** | Telnet                  | Supported since version 0.15.                                            |
| **Marlin**       | Serial Port             | Supported since version 1.7.2.                                           |
| **Ruida**        | USB Serial              | Experimental `.rd` program transfer; execution is not monitored.         |
| **Ruida**        | Network (UDP)           | Experimental `.rd` program transfer; no controller management or status. |
| **OctoPrint**    | Network (HTTP API)      | Connect through an OctoPrint server.                                     |

### Experimental Ruida Program Generation

Rayforge compiles complete Ruida `.rd` programs through
[ruida-re](https://github.com/ens1/ruida-re) and transfers them over USB
serial or UDP. The conservative, hardware-observed `proven` profile remains
the default. Advanced behavior must be enabled with an explicit research
profile. Narrow planned-path coupons have been run on a Boss LS2040 over USB
serial. A direct 10% coupon produced the expected motion without visible
marks, while direct and Rayforge-generated 15% single-section coupons produced
visible lines. A separate Rayforge 15% cross-hatch coupon executed two
five-mark diagonal sections at 100 mm/s; the operator reported both directions
visible and no connection burns. Its small top-left edge is a decoded 0.3507 mm
`cut_relative` mark, not evidence for pulse control.

Ruida's native grayscale `C7`/`C2` values are normalized positions within the
layer minimum/maximum range, not absolute output percentages. Rayforge's
`absolute_u8` scanline bytes are already resolved hardware outputs, so the
adapter inverse-normalizes each positive sample with
`(sample - minimum) / (maximum - minimum)` before compiling it. Passing an
absolute sample directly would apply the layer range a second time. Exact-zero
samples remain travel motion, and the observed raw-field floor is retained for
positive modulation. This mapping is supported by controlled LightBurn exports
and the producer contract. One exact horizontal variable-power native-raster
row at 100 mm/s and a requested 5%-15% layer range has now completed a
supervised Boss LS2040 transfer with the operator report, "Everything is as
expected". It used paired normalized `C7`/`C2` values, only `AA` marking chunks
no longer than 4 mm, and planned 23 mm and 26 mm marks separated by an 11 mm
travel gap. This is scoped visual evidence for that artifact, not dimensional
or power metrology, proof of zero optical output in the gap, or broad raster
compatibility.

A separate exact two-layer ordinary raster matrix at requested 20% power and
100 mm/s exercised horizontal and vertical bidirectional native raster. Its
marked motion used only `AA` and `AB` chunks no longer than 4 mm, and its gaps
used travel motion. After the supervised transfer, the operator confirmed the
expected three broken horizontal rows and three broken vertical columns. The
[scoped matrix manifest](tests/machine/driver/ruida/fixtures/hardware/boss-ls2040-usb-serial-rayforge-ordinary-raster-matrix-v1/manifest-v1.json)
binds that observation to the exact artifact. It is not dimensional or power
metrology, proof of zero optical output in the gaps, or unidirectional-raster
evidence.

A separate production-path coupon selected unidirectional scanning through the
serialized `EngraveStep` setting for one horizontal and one vertical native-
raster layer. At requested 20% power and 100 mm/s, its exact one-packet,
zero-retry transfer produced the operator report, "I see 12 lines, 2x3 vertical
and 2x3 horizontal, no burnt return moves, Z remained. All looks as expected".
The [scoped unidirectional manifest](tests/machine/driver/ruida/fixtures/hardware/boss-ls2040-usb-serial-rayforge-unidirectional-raster-v1/manifest-v1.json)
binds that observation to the 769-byte artifact. It is visual evidence for
those exact cardinal patterns and clean-looking returns, not directional,
dimensional, power, zero-output, or Z metrology.

Normal full-layer job-context air assist has a scoped positive observation on
the tested Boss LS2040. An exact air-off motion control produced an ambiguous
operator report of possible airflow masked by motor noise. Its paired 580-byte
air-on motion artifact was then transferred once with one host packet and zero
retries; the operator reported, "Air assist is confirmed, I felt the solenoid
turn on then off". A separately approved standalone `CA01` OFF-ON-OFF sequence
contained no motion, marking, or laser-enable commands and used a
5.002178-second host interval. All three USB-serial writes and flushes
completed, but the operator reported no change or relay/solenoid click. The
[scoped air-assist manifest](tests/machine/driver/ruida/fixtures/hardware/boss-ls2040-usb-serial-rayforge-air-assist-v1/manifest-v1.json)
records the exact bytes and observations. Serial provided no controller or
state acknowledgement. The full-layer result is tactile operator evidence, not
pressure, flow, timing, relay-routing, current, or electrical metrology, and it
does not establish other controllers or UDP. It does not validate the
standalone sequence as a manual toggle or change encoder or compiler behavior.

Four one-layer dynamic-vector coupons were also run on that machine at
100 mm/s. The first, planned as 15%-10%-15%, looked solid and did not establish
that power changed. On a longer 15%-5%-15% coupon, the operator observed good
motion but only the first 30 mm marked. Review of that exact payload found a
reduced-power envelope before the middle span and no baseline-power restore
before the final span. Rayforge therefore requires a restoration-capable
`ruida-re` compiler and rejects older compilers before transfer. A corrected
15%-5%-15% coupon with an explicit restore produced the operator-reported
result "a ~30mm line, a gap, and a ~30mm line." A fourth 15%-5%-15%-5%-15%
coupon encoded two reductions and two explicit restorations over five planned
16 mm spans; the operator reported three lines and two gaps. Those results
establish only the exact restore sequences on one machine. They are not
calibrated power or zero-output evidence, and the broad dynamic profile remains
research-only.

A subsequent nominal-0% four-side control emitted enough laser power to draw a
visible rectangle on the same Boss. The cause is unknown: raw zero may have
default, stale, or no-update semantics; a firing floor or another field may
contribute. Rayforge now rejects static marking motion at zero power, and the
`ruida-re` compiler rejects enabled marking power fields and raster modulation
below the observed raw-field floor of 16. That is a fail-closed generation
boundary, not proof that low positive power is physically safe. Existing or
externally supplied `.rd` files receive no retroactive protection. The paired
200 ms C6 11 dwell job and earlier planned Z coupons were never sent and
remain untested and quarantined. A later positive-power, travel-only staged
test used one and four exact 100 ms C6 11 delays after absolute travel moves. The
operator reported corner pauses and only the intentional faint 5 mm anchor
mark. This is observation for those exact files, not timing metrology or broad
dwell validation. Research profiles emit a warning and reject requests
outside their narrow evidence before transfer.

Two later 673-byte native-raster jobs exercised typed logical Z offsets of
+1.0 and -1.0 mm. Each was transferred once in one host-reported packet with
zero retries and no controller or execution acknowledgement. Starting from a
reported 18.2 mm machine Z readout, the operator observed 17.2 mm during the
positive job and 19.2 mm during the negative job, with both returning to
18.2 mm. The negative job's marks were also reported as expected, with no
collision or unexpected movement. The
[paired logical-Z manifest](tests/machine/driver/ruida/fixtures/hardware/boss-ls2040-usb-serial-rayforge-logical-z-v1/manifest-v1.json)
binds those reports to the exact payloads. This is controller-readout evidence,
not independent displacement, physical-direction, accuracy, backlash, or
repeatability metrology.

The opt-in profiles cover constant-power diagonal or cross-hatch planned-path
raster, selecting either Ruida laser channel 1 or 2 (never both at once),
vector Dwell from manual Ops or frame corner pauses, 10-20 kHz RF frequency,
0-0.2 µs (0-200 ns) fiber pulse width, a balanced logical Z offset of up to
±1 mm for one native raster layer, and reduced vector power at tabs. Research
profiles are not composable. The exact travel-then-100 ms dwell subset and the
paired ±1 mm logical-Z controller-readout subset are operator-observed.
Mark-adjacent dwell, 200 ms dwell, stationary marking Pulse, interrupted Z
restoration, other Z offsets or layer structures, and physical Z semantics
remain unvalidated. Rotary, cut-through controls, generic endpoint Z motion,
and other unobserved combinations remain unsupported.

Ruida drivers are transfer-only. They do not manage the controller or monitor
job execution, so a successful transfer is not confirmation that cutting or
engraving has finished. See the
[firmware reference](website/docs/reference/firmware.md#ruida-controllers) for
the exact profile scopes and lifecycle.

Before releasing this integration, `ruida-re` 0.1.0 must be published; the
repository pin used for development is not a substitute for the public package
declared by Rayforge.

## Installation

For installation instructions [refer to our homepage](https://rayforge.org/docs/getting-started/installation).

## Development

For detailed information about developing for Rayforge, including setup instructions,
testing, and contribution guidelines, please see the
[Developer Documentation](https://rayforge.org/docs/developer/getting-started).

## License

This project is licensed under the **MIT License**. See the `LICENSE` file for details.
