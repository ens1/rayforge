# Scoped Boss LS2040 travel-then-dwell evidence

This directory preserves three exact Rayforge-generated artifacts used in a
staged, supervised test on one operator-identified Boss LS2040. Each file was
transferred once over USB serial after explicit approval. Every host transfer
reported one packet and zero retries; the transport provided no controller or
execution acknowledgement.

All three files contain the same single planned 5 mm anchor at 15% requested
power and 100 mm/s, followed only by four absolute travel moves. The control
contains no dwell. The sentinel adds one 100 ms `C611` `additional_delay`
immediately after the first travel. The full coupon adds the same delay after
each of the four travels. No file contains a `C610` pulse, and there is no
marking command after the anchor.

The operator reported the following results, in stage order:

- "I see one faint line, vertical, about 5mm"
- "It looks like it did a rectangle with pauses at the corner? Nothing other
  than a horizontal line, about 5mm"
- "Yes, one faint line, pauses at the corners"

These reports provide scoped evidence that pause behavior was visible during
the exact one- and four-delay travel coupons without a visible post-anchor
mark. They are not timing, dimensional, motion, optical-power, or electrical
metrology. In particular, they do not establish a measured 100 ms duration,
mark-adjacent dwell, 200 ms dwell, stationary marking pulse, arbitrary dwell
sequences, or broad `stationary-research` profile compatibility.
