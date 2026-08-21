# Q2680: Parser accepts trailing/garbage bytes in IPv6FindUpperProtocol

## Question
Does `IPv6FindUpperProtocol` (iputil/packet.go) accept a packet containing a message type outside the known enum plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `iputil/packet.go` -> `IPv6FindUpperProtocol` (declared at iputil/packet.go:349)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `IPv6FindUpperProtocol` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `IPv6FindUpperProtocol` disagrees (rejects the second).
