# Q2523: Query amplification through BalancePacket

## Question
Can one small unauthenticated packet containing an advertised private/loopback remote make `BalancePacket` (routing/balance.go) emit a substantially larger or multiplied response?

## Target
- File/function: `routing/balance.go` -> `BalancePacket` (declared at routing/balance.go:27)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `BalancePacket`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `BalancePacket`, asserting it stays at or below 1.
