# Q3455: Error path leaves partial state in Interface.readOutsidePackets

## Question
When `Interface.readOutsidePackets` (outside.go) errors out on a length field larger than the datagram, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `outside.go` -> `Interface.readOutsidePackets` (declared at outside.go:26)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a length field larger than the datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `Interface.readOutsidePackets`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `Interface.readOutsidePackets` on failing input then valid input and asserts identical results to a fresh instance.
