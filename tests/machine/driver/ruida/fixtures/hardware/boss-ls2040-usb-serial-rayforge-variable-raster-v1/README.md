# Scoped Boss LS2040 variable-raster evidence

This directory preserves the exact Rayforge-generated native-raster artifact
described by `manifest-v1.json`. After explicit approval, the 555-byte file
was transferred once to one operator-identified Boss LS2040 over USB serial.
The host reported one packet and zero retries; the transport did not provide a
controller or execution acknowledgement. After being asked about the two
expected lines, their gap, and any unexpected marking or motion, the operator
reported, "Everything is as expected".

The decoded plan is one horizontal, bidirectional native-raster row at
100 mm/s with a requested 5%-15% layer range. Its paired `C7`/`C2` records
contain normalized range positions of approximately 99.017%, 24.507%,
24.507%, and 99.017%. Under the decoded layer range, the largest modeled
effective output is approximately 14.899%. The planned row contains a 23 mm
mark, an 11 mm semantic `TravelTo` gap, and a 26 mm mark within X 80 through
140 mm at Y 24.052 mm. All marked motion uses `AA` `cut_horizontal` chunks no
longer than 4 mm; no generic `A8` or `A9` cut appears.

This is positive operator-observed evidence only for the expected appearance
of this exact one-row artifact, machine, material, settings, and supervised
transfer. The decoded dimensions and effective outputs are not operator
measurements. No dimensional, optical-power, electrical, timing, or motion
instrumentation was used, and the report does not establish that the two
positive modulation levels were visually distinguishable. It does not prove
zero optical output in the travel gap or validate other rows, axes, scan
strategies, powers, speeds, layers, controllers, or transports. It does not
promote a broader profile.

The earlier mixed vector/native-raster negative artifact remains quarantined
and is not superseded. This coupon retained its approximately 14.899% maximum
modeled output and variable modulation, but used bounded native `AA` marking
throughout, a higher 5% layer minimum, and only one raster row. Those remaining
differences prevent a single-factor causal conclusion about the earlier blank
raster rows.
