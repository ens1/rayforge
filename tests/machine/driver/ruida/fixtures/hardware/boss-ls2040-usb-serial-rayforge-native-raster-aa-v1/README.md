# Scoped Boss LS2040 native-raster AA evidence

This directory preserves the exact Rayforge-generated native-raster artifact
described by `manifest-v1.json`. After explicit approval, the 525-byte file
was transferred once to one operator-identified Boss LS2040 over USB serial.
The host reported one packet and zero retries; the transport did not provide a
controller or execution acknowledgement. The operator then reported,
"Everything looks right to me".

The decoded plan is one horizontal, constant-power native-raster row at
100 mm/s and 20% requested power. It contains a planned 25 mm mark, an 11 mm
semantic `TravelTo` gap, and a planned 24 mm mark within X 60 through 120 mm
at Y 48.052 mm. The two marks are serialized as thirteen `AA`
`cut_horizontal` chunks no longer than 4 mm. The artifact contains no `A8` or
`A9` cut and no `C7` or `C2` immediate modulation record.

This is operator-observed evidence only for the exact constant-power artifact,
machine, material, settings, and supervised transfer recorded in the manifest.
The reported appearance is not dimensional, optical-power, electrical, or
timing metrology. It does not validate variable-power raster modulation,
arbitrary scan strategies, other powers or speeds, another controller, or
execution monitoring.

The earlier mixed vector/native-raster artifact remains separate, quarantined
negative evidence. This successful discriminator changed more than one factor:
it used native AA chunks throughout, constant 20% power without modulation,
and one raster row. It therefore does not isolate which difference caused the
earlier raster row to move without leaving a visible mark.
