# Scoped Boss LS2040 air-assist evidence

This directory preserves an inconclusive motion-job control and a negative
standalone air-control observation from one supervised Boss LS2040 session.
It does not establish that controller-controlled air works on this setup.

The 580-byte air-off motion control was transferred once. The host reported
one packet and zero retries, without controller or execution acknowledgement.
The operator reported, "I think it had air flow, but it's hard to tell with
the sound of the motors." That observation cannot distinguish commanded air
from pre-existing flow, motor noise, or another machine-side condition. The
otherwise paired air-on motion artifact was never transmitted.

After explicit approval, a standalone sequence wrote three exact scrambled
three-byte commands over USB serial: pre-OFF `c4099b`, ON `c4091b`, and final
OFF `c4099b`. Their logical forms are `ca0112`, `ca0113`, and `ca0112`. Each
contains one structured `CA01` `layer_control` record and no motion, mark,
laser-enable, power, dwell, pulse, or job-envelope command. The measured host
interval was 5.002178 seconds. All three host writes and flushes completed;
the serial link provided no controller acknowledgement or state reply.

The operator reported verbatim:

> No motion or emission.
>
> No change, no relay clicks, nothing. But lightburn also seems to have failed
> on this. I could have bad hardware. I would expect to hear a relay or
> solenoid click

No physical response was observed. That does not prove whether the controller
parsed or ignored the standalone commands, whether this controller requires a
full job envelope, or whether the air path was already on, bypassed, disabled,
misconfigured, disconnected, or faulty. The LightBurn comparison and possible
bad hardware are operator reports that make a machine-side issue plausible;
they do not establish causality. Controller-controlled air therefore remains
unavailable or inconclusive on this exact setup. These observations do not
promote standalone air control or broaden compiler or encoder behavior.
