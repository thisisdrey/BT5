# Q2772: Parser accepts trailing/garbage bytes in createICMPv6EchoResponse

## Question
Does `createICMPv6EchoResponse` (iputil/packet.go) accept a packet containing header.Type plus unparsed trailing bytes, allowing an attacker to smuggle content past the length checks?

## Target
- File/function: `iputil/packet.go` -> `createICMPv6EchoResponse` (declared at iputil/packet.go:443)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: header.Type; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Append attacker bytes after the structurally valid region and see whether `createICMPv6EchoResponse` still returns success.
- Invariant to test: A packet is accepted only if every received byte is consumed by the parse, with no unexamined remainder.
- Expected Immunefi impact: Packet-parsing confusion enabling firewall evasion or downstream misinterpretation of attacker data.
- Fast validation: Differential unit test: parse the clean packet and the packet+trailer, assert `createICMPv6EchoResponse` disagrees (rejects the second).
