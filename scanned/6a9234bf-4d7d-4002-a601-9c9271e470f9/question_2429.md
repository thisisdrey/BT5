# Q2429: Query amplification through hashPacket

## Question
Can one small unauthenticated packet containing a spoofed UDP source address make `hashPacket` (routing/balance.go) emit a substantially larger or multiplied response?

## Target
- File/function: `routing/balance.go` -> `hashPacket` (declared at routing/balance.go:14)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `hashPacket`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `hashPacket`, asserting it stays at or below 1.
