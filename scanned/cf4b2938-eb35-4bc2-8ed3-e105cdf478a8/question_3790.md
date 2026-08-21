# Q3790: Query amplification through relayManager.handleCreateRelayRequest

## Question
Can one small unauthenticated packet containing a preferred-ranges entry make `relayManager.handleCreateRelayRequest` (relay_manager.go) emit a substantially larger or multiplied response?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayRequest` (declared at relay_manager.go:426)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `relayManager.handleCreateRelayRequest`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `relayManager.handleCreateRelayRequest`, asserting it stays at or below 1.
