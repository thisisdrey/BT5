# Q2586: Parser accepts trailing/garbage bytes in ipv6CreateRejectPacket

## Question
Does `ipv6CreateRejectPacket` (iputil/packet.go) accept a packet containing a length field larger than the datagram plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `iputil/packet.go` -> `ipv6CreateRejectPacket` (declared at iputil/packet.go:206)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a length field larger than the datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `ipv6CreateRejectPacket` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `ipv6CreateRejectPacket` disagrees (rejects the second).
