# Q1818: Unbounded allocation from wire input in ipv6CreateRejectICMPPacket

## Question
Can an unprivileged attacker make `ipv6CreateRejectICMPPacket` (iputil/packet.go) allocate or copy memory proportional to an IPv4 packet with IHL=15 and no options rather than to the bytes actually received?

## Target
- File/function: `iputil/packet.go` -> `ipv6CreateRejectICMPPacket` (declared at iputil/packet.go:219)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a small datagram that declares a very large size and count the allocation performed inside `ipv6CreateRejectICMPPacket`.
- Invariant to test: Allocation size is bounded by the received datagram length and a hard maximum, never by an attacker-declared field.
- Expected Immunefi impact: Remote memory exhaustion of a node from cheap unauthenticated packets, an availability loss for every peer of that node.
- Fast validation: Benchmark/unit test asserting peak allocation in `ipv6CreateRejectICMPPacket` stays under a fixed bound for a 64-byte datagram declaring a 4GB length.
