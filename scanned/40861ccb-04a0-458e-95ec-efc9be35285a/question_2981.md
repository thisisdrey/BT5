# Q2981: Error path leaves partial state in GenericConn.Rebind

## Question
When `GenericConn.Rebind` (udp/udp_android.go) errors out on header.Version set to an unknown value, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `udp/udp_android.go` -> `GenericConn.Rebind` (declared at udp/udp_android.go:43)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Version set to an unknown value; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `GenericConn.Rebind`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `GenericConn.Rebind` on failing input then valid input and asserts identical results to a fresh instance.
