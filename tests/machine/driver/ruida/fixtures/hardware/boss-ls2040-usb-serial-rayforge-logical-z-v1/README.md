# Scoped Boss LS2040 logical-Z readout evidence

This directory preserves the two exact Rayforge-generated native-raster
artifacts described by `manifest-v1.json`. They were generated with typed
logical Z offsets of +1.0 and -1.0 mm under the opt-in `z-research` profile.
The positive job contains an `80 03` entry delta of -1.0 mm before marking and
an equal +1.0 mm restore. The negative job reverses that pair.

After separate explicit approvals, each 673-byte artifact was transferred
exactly once to one operator-identified Boss LS2040 through the stock Rayforge
`RuidaSerialDriver` over USB serial. For each transfer, the stock driver
reported:

> Ruida program transfer completed: 1 packet(s), 0 retry(s). Controller
> execution is not monitored.

The serial path supplied no controller or execution acknowledgement. During
the positive logical-Z job, the operator watched the machine's Z readout and
reported, "It went to 17.2mm during cutting, then returned to 18.2". During the
negative logical-Z job, the operator reported, "Yes, went to 19.2 and is now
at 18.2. No collision or unexpected movement. Marks look great, as expected".
Together these are scoped observations of opposite 1.0 mm readout changes and
numerical returns to the reported 18.2 mm starting value for the two exact
completed jobs. The negative report also confirms its expected-looking marks
and no operator-observed collision or unexpected movement.

The readout was not independent position instrumentation. The observation does
not establish physical displacement accuracy, backlash, repeatability, which
machine component moved, or the physical direction associated with either
readout sign. It does not validate an interrupted or failed restore, other
offsets, other layers, other controllers, or UDP. The positive response did
not separately confirm its decoded marking pattern, and the negative response
is qualitative appearance evidence rather than dimensional or power
metrology. The `z-research` profile therefore remains opt-in and
evidence-limited; this result does not promote the default profile or change
encoder or compiler behavior.
