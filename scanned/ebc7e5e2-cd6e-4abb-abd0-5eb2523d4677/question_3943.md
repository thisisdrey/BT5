# Q3943: Error path leaves partial state in ipv6PseudoheaderChecksum

## Question
When `ipv6PseudoheaderChecksum` (iputil/packet.go) errors out on an IPv6 packet with chained extension headers, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `iputil/packet.go` -> `ipv6PseudoheaderChecksum` (declared at iputil/packet.go:520)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `ipv6PseudoheaderChecksum`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `ipv6PseudoheaderChecksum` on failing input then valid input and asserts identical results to a fresh instance.
