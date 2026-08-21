# Q1905: Unbounded allocation from wire input in SubTypeName

## Question
Can an unprivileged attacker make `SubTypeName` (header/header.go) allocate or copy memory proportional to an inner protocol byte of 0xFF rather than to the bytes actually received?

## Target
- File/function: `header/header.go` -> `SubTypeName` (declared at header/header.go:182)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `SubTypeName`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `SubTypeName` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
