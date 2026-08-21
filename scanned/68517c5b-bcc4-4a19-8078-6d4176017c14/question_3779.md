# Q3779: Error path leaves partial state in IPv6FindUpperProtocol

## Question
When `IPv6FindUpperProtocol` (iputil/packet.go) errors out on header.Type, does it leave partially updated buffers, counters, or index entries that the next packet can observe?

## Target
- File/function: `iputil/packet.go` -> `IPv6FindUpperProtocol` (declared at iputil/packet.go:349)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Type; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an input that fails midway through `IPv6FindUpperProtocol`, then a valid packet, and compare behaviour to a clean node.
- Invariant to test: Every failure path in the receive loop restores the exact pre-packet state.
- Expected Immunefi impact: State corruption exploitable for tunnel wedging or cross-session data mixing.
- Fast validation: Unit test that runs `IPv6FindUpperProtocol` on failing input then valid input and asserts identical results to a fresh instance.
