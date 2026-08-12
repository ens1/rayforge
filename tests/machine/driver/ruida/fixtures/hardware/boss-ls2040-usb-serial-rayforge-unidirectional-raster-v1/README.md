# Scoped Boss LS2040 unidirectional raster evidence

This directory preserves the exact Rayforge-generated two-layer native-raster
coupon described by `manifest-v1.json`. It was generated through the
production `EngraveStep` setting and serialization path with unidirectional
scanning selected for both layers. The resulting job was independently decoded
before transmission.

After explicit approval, the 769-byte artifact was transferred exactly once to
one operator-identified Boss LS2040 over USB serial. The stock driver reported
one packet and zero retries; the transport provided no controller or execution
acknowledgement. The operator then reported, "I see 12 lines, 2x3 vertical and
2x3 horizontal, no burnt return moves, Z remained. All looks as expected".

The exact artifact contains horizontal- and vertical-unidirectional
constant-power native-raster layers at 100 mm/s and requested 20% power. The
horizontal layer uses `CA41` mode 1, `CA01` operation 2, and only `AA` marks in
one signed direction. The vertical layer uses mode 3, operation 4, and only
`AB` marks in one signed direction. Its row and column returns are travel
motions. Every marked chunk is no longer than 4 mm. There is no Z, dwell,
immediate modulation, pulse, frequency, air-assist, rotary, or research
command.

This is scoped positive evidence for the expected visible horizontal and
vertical unidirectional patterns, clean-looking return travel, and unchanged
controller Z readout during this exact artifact, placement, machine, material,
settings, and supervised transfer. The geometry, powers, mode values, and
marking directions are decoded serialization facts, not physical measurements.
No dimensional, optical-power, electrical, timing, or motion instrumentation
was used. Clean-looking returns do not prove zero optical output, and the
unchanged display does not constitute calibrated Z metrology. This result does
not establish other powers, speeds, angles, layers, controllers, transports, or
broad profile compatibility.
