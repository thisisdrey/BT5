# Q1828: Unbounded allocation from wire input in Interface.handleOutsideMessagePacket

## Question
Can an unprivileged attacker make `Interface.handleOutsideMessagePacket` (outside.go) allocate or copy memory proportional to a message type outside the known enum rather than to the bytes actually received?

## Target
- File/function: `outside.go` -> `Interface.handleOutsideMessagePacket` (declared at outside.go:450)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `Interface.handleOutsideMessagePacket`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `Interface.handleOutsideMessagePacket` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
