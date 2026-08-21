# Q3287: Panic on malformed input path in ipv4CreateRejectICMPPacket

## Question
Can a length field larger than the datagram reach `ipv4CreateRejectICMPPacket` (iputil/packet.go) and trigger a nil dereference, slice bound panic, or type assertion failure in the hot receive path?

## Target
- File/function: `iputil/packet.go` -> `ipv4CreateRejectICMPPacket` (declared at iputil/packet.go:63)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a length field larger than the datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Fuzz the receive path with structurally malformed datagrams and observe crashes in `ipv4CreateRejectICMPPacket`.
- Invariant to test: The receive path never panics on any byte sequence; malformed input yields a logged drop.
- Expected Immunefi impact: Single-packet remote denial of service of a node, disconnecting all of its tunnels.
- Fast validation: Go fuzz target over `ipv4CreateRejectICMPPacket` run to 1M execs with `-race`, asserting zero crashes.
