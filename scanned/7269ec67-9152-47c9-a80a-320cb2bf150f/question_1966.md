# Q1966: Parser accepts trailing/garbage bytes in NewListener

## Question
Does `NewListener` (udp/udp_windows.go) accept a packet containing a 0-byte datagram plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `udp/udp_windows.go` -> `NewListener` (declared at udp/udp_windows.go:14)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a 0-byte datagram; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `NewListener` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `NewListener` disagrees (rejects the second).
