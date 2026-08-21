# Q2770: Parser accepts trailing/garbage bytes in CreateICMPEchoResponse

## Question
Does `CreateICMPEchoResponse` (iputil/packet.go) accept a packet containing an inner protocol byte of 0xFF plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `iputil/packet.go` -> `CreateICMPEchoResponse` (declared at iputil/packet.go:394)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: an inner protocol byte of 0xFF; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `CreateICMPEchoResponse` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `CreateICMPEchoResponse` disagrees (rejects the second).
