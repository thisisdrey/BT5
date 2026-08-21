# Q2479: Parser accepts trailing/garbage bytes in H.Parse

## Question
Does `H.Parse` (header/header.go) accept a packet containing a fragmented inner packet with offset!=0 plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `header/header.go` -> `H.Parse` (declared at header/header.go:143)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a fragmented inner packet with offset!=0; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `H.Parse` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `H.Parse` disagrees (rejects the second).
