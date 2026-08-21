# Q3778: Error path leaves partial state in ipv6CreateRejectTCPPacket

## Question
When `ipv6CreateRejectTCPPacket` (iputil/packet.go) errors out on an inner packet claiming 65535 bytes, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `iputil/packet.go` -> `ipv6CreateRejectTCPPacket` (declared at iputil/packet.go:275)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `ipv6CreateRejectTCPPacket`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `ipv6CreateRejectTCPPacket` on failing input then valid input and asserts identical results to a fresh instance.
