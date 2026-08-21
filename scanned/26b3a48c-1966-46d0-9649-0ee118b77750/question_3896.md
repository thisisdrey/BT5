# Q3896: Parser accepts trailing/garbage bytes in GenericConn.LocalAddr

## Question
Does `GenericConn.LocalAddr` (udp/udp_generic.go) accept a packet containing an IPv4 packet with IHL=15 and no options plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `udp/udp_generic.go` -> `GenericConn.LocalAddr` (declared at udp/udp_generic.go:47)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an IPv4 packet with IHL=15 and no options; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `GenericConn.LocalAddr` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `GenericConn.LocalAddr` disagrees (rejects the second).
