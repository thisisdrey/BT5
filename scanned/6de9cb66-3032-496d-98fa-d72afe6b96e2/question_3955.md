# Q3955: Concurrent map mutation in relayManager.EstablishRelay

## Question
Can attacker-paced an oversized remote list race concurrent readers of the structures `relayManager.EstablishRelay` (relay_manager.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `relay_manager.go` -> `relayManager.EstablishRelay` (declared at relay_manager.go:271)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `relayManager.EstablishRelay` concurrently, asserting no fatal error.
