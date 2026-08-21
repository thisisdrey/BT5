# Q3539: Error path leaves partial state in newPacket

## Question
When `newPacket` (outside.go) errors out on an inner packet claiming 65535 bytes, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `outside.go` -> `newPacket` (declared at outside.go:306)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `newPacket`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `newPacket` on failing input then valid input and asserts identical results to a fresh instance.
