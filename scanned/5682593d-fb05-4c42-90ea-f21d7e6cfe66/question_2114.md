# Q2114: Unbounded allocation from wire input in ipv6PseudoheaderChecksum

## Question
Can an unprivileged attacker make `ipv6PseudoheaderChecksum` (iputil/packet.go) allocate or copy memory proportional to header.Subtype rather than to the bytes actually received?

## Target
- File/function: `iputil/packet.go` -> `ipv6PseudoheaderChecksum` (declared at iputil/packet.go:520)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Subtype; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `ipv6PseudoheaderChecksum`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `ipv6PseudoheaderChecksum` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
