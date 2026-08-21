# Q2498: Parser accepts trailing/garbage bytes in newPacket

## Question
Does `newPacket` (outside.go) accept a packet containing header.Version set to an unknown value plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `outside.go` -> `newPacket` (declared at outside.go:306)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Version set to an unknown value; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `newPacket` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `newPacket` disagrees (rejects the second).
