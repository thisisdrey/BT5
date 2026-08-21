# Q2809: Unbounded allocation from wire input in GenericConn.ReloadConfig

## Question
Can an unprivileged attacker make `GenericConn.ReloadConfig` (udp/udp_generic.go) allocate or copy memory proportional to an IPv6 packet with chained extension headers rather than to the bytes actually received?

## Target
- File/function: `udp/udp_generic.go` -> `GenericConn.ReloadConfig` (declared at udp/udp_generic.go:63)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `GenericConn.ReloadConfig`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `GenericConn.ReloadConfig` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
