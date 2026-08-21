# Q2592: Parser accepts trailing/garbage bytes in Interface.handleOutsideMessagePacket

## Question
Does `Interface.handleOutsideMessagePacket` (outside.go) accept a packet containing an inner packet claiming 65535 bytes plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `outside.go` -> `Interface.handleOutsideMessagePacket` (declared at outside.go:450)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner packet claiming 65535 bytes; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `Interface.handleOutsideMessagePacket` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `Interface.handleOutsideMessagePacket` disagrees (rejects the second).
