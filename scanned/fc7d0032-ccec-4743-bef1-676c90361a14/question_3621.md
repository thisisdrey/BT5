# Q3621: Error path leaves partial state in Interface.handleOutsideMessagePacket

## Question
When `Interface.handleOutsideMessagePacket` (outside.go) errors out on header.RemoteIndex, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `outside.go` -> `Interface.handleOutsideMessagePacket` (declared at outside.go:450)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.RemoteIndex; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `Interface.handleOutsideMessagePacket`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `Interface.handleOutsideMessagePacket` on failing input then valid input and asserts identical results to a fresh instance.
