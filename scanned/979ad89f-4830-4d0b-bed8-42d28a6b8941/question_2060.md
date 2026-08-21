# Q2060: Parser accepts trailing/garbage bytes in NewListenConfig

## Question
Does `NewListenConfig` (udp/udp_bsd.go) accept a packet containing an IPv6 packet with chained extension headers plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `udp/udp_bsd.go` -> `NewListenConfig` (declared at udp/udp_bsd.go:23)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `NewListenConfig` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `NewListenConfig` disagrees (rejects the second).
