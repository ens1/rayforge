# Scoped Boss LS2040 ordinary raster-matrix evidence

This directory preserves the exact Rayforge-generated ordinary native-raster
matrix described by `manifest-v1.json`. The operator replaced the cardboard
with a blank piece while retaining the reported top-right 0,0 origin, then
requested placement approximately 3 inches in both physical directions from
that origin. The serialized controller bounds begin at X 76.2 and Y 76.2 mm.
Those coordinates are decoded job data, not a physical placement measurement.

After explicit approval, the 746-byte artifact was transferred once to one
operator-identified Boss LS2040 over USB serial. The host reported one packet
and zero retries; the transport provided no controller or execution
acknowledgement. When asked to confirm three broken horizontal rows, three
broken vertical columns, clean gaps, and no unexpected motion or marking, the
operator reported, "Yes, that is what I see".

The exact artifact contains two constant-power native-raster layers at
100 mm/s and requested 20% power. Layer 0 is horizontal bidirectional and
uses only `AA` marking chunks; layer 1 is vertical bidirectional and uses only
`AB` marking chunks. Every marked chunk is no longer than 4 mm. Gaps use
travel motion, and no `C7`, `C2`, `A8`, or `A9` opcode appears.

This is positive visual evidence only for the expected appearance of these
two layers in this exact artifact, placement, machine, material, settings,
and supervised transfer. The decoded geometry and quantized power are not
operator measurements. No dimensional, optical-power, electrical, timing, or
motion instrumentation was used. A clean-looking gap does not prove zero
optical output during travel. The artifact does not exercise unidirectional,
variable-power, or grayscale raster and does not establish other powers,
speeds, layers, controllers, transports, or broad profile compatibility.
