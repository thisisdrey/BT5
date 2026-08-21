# Q3813: Unbounded allocation from wire input in NewUDPStatsEmitter

## Question
Can an unprivileged attacker make `NewUDPStatsEmitter` (udp/udp_darwin.go) allocate or copy memory proportional to a fragmented inner packet with offset!=0 rather than to the bytes actually received?

## Target
- File/function: `udp/udp_darwin.go` -> `NewUDPStatsEmitter` (declared at udp/udp_darwin.go:163)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a fragmented inner packet with offset!=0; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `NewUDPStatsEmitter`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `NewUDPStatsEmitter` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
