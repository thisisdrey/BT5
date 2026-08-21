# Q3941: Error path leaves partial state in tcpipChecksum

## Question
When `tcpipChecksum` (iputil/packet.go) errors out on a truncated 15-byte datagram, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `iputil/packet.go` -> `tcpipChecksum` (declared at iputil/packet.go:485)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a truncated 15-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `tcpipChecksum`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `tcpipChecksum` on failing input then valid input and asserts identical results to a fresh instance.
