# Q2862: Parser accepts trailing/garbage bytes in ipv6PseudoheaderChecksum

## Question
Does `ipv6PseudoheaderChecksum` (iputil/packet.go) accept a packet containing header.MessageCounter plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `iputil/packet.go` -> `ipv6PseudoheaderChecksum` (declared at iputil/packet.go:520)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.MessageCounter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `ipv6PseudoheaderChecksum` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `ipv6PseudoheaderChecksum` disagrees (rejects the second).
