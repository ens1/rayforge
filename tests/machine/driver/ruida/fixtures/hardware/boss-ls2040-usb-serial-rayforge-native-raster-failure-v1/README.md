# Scoped Boss LS2040 native-raster negative evidence

This directory records one supervised execution of the exact 917-byte mixed
vector/native-raster artifact named in `manifest-v1.json`. The stored machine
file has a `.rd.quarantined` suffix and is retained as do-not-resend evidence.
Its SHA-256 is
`ee1ae328fd431b75f43f0bf61feaf1e08e4ef511fb0c3615d95b0d38aab5e3b4`.

The operator saw the expected first vector row with three separated marks.
The operator then saw raster motion but no visible raster mark. The initial
description, `Only one row is showing, 3 lines about 30cm`, was explicitly
corrected to `yes, 30mm/3cm`. Those dimensions are approximate visual
descriptions, not metrology.

The host reported one packet and zero retries. The serial path supplied no
controller or execution acknowledgement, so that summary is not evidence of
controller receipt or completion.

The decoded raster layer selected the LightBurn-observed horizontal
bidirectional mode pair. Each of its six higher-modulation spans was encoded
as `A8` `cut_absolute`, at lengths 24, 17, 23, 23, 17, and 24 mm. Its eight
`AA` `cut_horizontal` spans were only the 1 mm low-modulation antialias edges.
That correlation motivates follow-up discrimination, but it does not prove
that absolute cuts caused the missing raster output. Modulation and layer
range interaction, scan-mode compatibility, and the physical firing threshold
also remain unisolated.

This result passes only the exact vector row and fails only the visible-output
expectation for the native-raster portion of this artifact. It is not evidence
that all native raster, the serial transport, the controller family, or a
broad job profile fails.
