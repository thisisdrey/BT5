# Q3707: Query amplification through AddRelay

## Question
Can one small unauthenticated packet containing a HostUpdateNotification for another host's address make `AddRelay` (relay_manager.go) emit a substantially larger or multiplied response?

## Target
- File/function: `relay_manager.go` -> `AddRelay` (declared at relay_manager.go:229)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `AddRelay`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `AddRelay`, asserting it stays at or below 1.
