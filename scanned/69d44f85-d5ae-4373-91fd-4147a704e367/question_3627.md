# Q3627: Query amplification through relayManager.StartRelays

## Question
Can one small unauthenticated packet containing a HostQueryReply for an unrequested VPN address make `relayManager.StartRelays` (relay_manager.go) emit a substantially larger or multiplied response?

## Target
- File/function: `relay_manager.go` -> `relayManager.StartRelays` (declared at relay_manager.go:61)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Measure response bytes and packet count versus request size for `relayManager.StartRelays`.
- Invariant to test: Response size and count are bounded relative to the request and gated on authentication.
- Expected Immunefi impact: Reflection/amplification DDoS using Nebula nodes against third parties.
- Fast validation: Benchmark test measuring the amplification factor of `relayManager.StartRelays`, asserting it stays at or below 1.
