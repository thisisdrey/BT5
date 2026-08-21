# Q1177: Error path leaves partial state in StdConn.PrepareRawMessages

## Question
When `StdConn.PrepareRawMessages` (udp/udp_linux_32.go) errors out on a length field larger than the datagram, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `udp/udp_linux_32.go` -> `StdConn.PrepareRawMessages` (declared at udp/udp_linux_32.go:33)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a length field larger than the datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `StdConn.PrepareRawMessages`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `StdConn.PrepareRawMessages` on failing input then valid input and asserts identical results to a fresh instance.
