# Q2385: Parser accepts trailing/garbage bytes in H.String

## Question
Does `H.String` (header/header.go) accept a packet containing an IPv6 packet with chained extension headers plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `header/header.go` -> `H.String` (declared at header/header.go:113)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv6 packet with chained extension headers; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `H.String` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `H.String` disagrees (rejects the second).
