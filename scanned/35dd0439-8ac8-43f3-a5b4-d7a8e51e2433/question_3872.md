# Q3872: Concurrent map mutation in relayManager.GetAmRelay

## Question
Can attacker-paced a relay request for a host it does not own race concurrent readers of the structures `relayManager.GetAmRelay` (relay_manager.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `relay_manager.go` -> `relayManager.GetAmRelay` (declared at relay_manager.go:49)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `relayManager.GetAmRelay` concurrently, asserting no fatal error.
