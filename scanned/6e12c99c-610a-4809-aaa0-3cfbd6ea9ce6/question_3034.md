# Q3034: Inner-packet parse desync in ipv6CreateRejectTCPPacket

## Question
Can an unprivileged attacker use a message type outside the known enum so `ipv6CreateRejectTCPPacket` (iputil/packet.go) derives a different view of the inner packet than the firewall check does later in the pipeline?

## Target
- File/function: `iputil/packet.go` -> `ipv6CreateRejectTCPPacket` (declared at iputil/packet.go:275)
- Entrypoint: Unauthenticated UDP datagram delivered to the listener and dispatched through `Interface.readOutsidePackets`
- Attacker controls: a message type outside the known enum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Build an inner packet that two parsers read differently, then confirm the firewall sees the benign interpretation and the tun device the malicious one.
- Invariant to test: Exactly one parse of the inner packet is performed and its result is the value used by both the firewall decision and the write.
- Expected Immunefi impact: Firewall policy bypass letting attacker traffic reach an inside service the rules deny.
- Fast validation: Differential test comparing the firewall's parsed `firewall.Packet` against the bytes actually written to the tun device for the same input.
