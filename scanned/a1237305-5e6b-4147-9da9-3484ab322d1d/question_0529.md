# Q0529: Unbounded allocation from wire input in StdConn.PrepareRawMessages

## Question
Can an unprivileged attacker make `StdConn.PrepareRawMessages` (udp/udp_linux_32.go) allocate or copy memory proportional to header.MessageCounter rather than to the bytes actually received?

## Target
- File/function: `udp/udp_linux_32.go` -> `StdConn.PrepareRawMessages` (declared at udp/udp_linux_32.go:33)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.MessageCounter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `StdConn.PrepareRawMessages`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `StdConn.PrepareRawMessages` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
