# Q2497: Parser accepts trailing/garbage bytes in Interface.handleHostRoaming

## Question
Does `Interface.handleHostRoaming` (outside.go) accept a packet containing a fragmented inner packet with offset!=0 plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `outside.go` -> `Interface.handleHostRoaming` (declared at outside.go:265)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a fragmented inner packet with offset!=0; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `Interface.handleHostRoaming` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `Interface.handleHostRoaming` disagrees (rejects the second).
