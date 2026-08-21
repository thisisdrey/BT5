# Q3708: Query amplification through relayManager.EstablishRelay

## Question
Can one small unauthenticated packet containing a punch notification naming an arbitrary target make `relayManager.EstablishRelay` (relay_manager.go) emit a substantially larger or multiplied response?

## Target
- File/function: `relay_manager.go` -> `relayManager.EstablishRelay` (declared at relay_manager.go:271)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `relayManager.EstablishRelay`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `relayManager.EstablishRelay`, asserting it stays at or below 1.
