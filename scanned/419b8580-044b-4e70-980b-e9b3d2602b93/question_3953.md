# Q3953: Concurrent map mutation in relayManager.StartRelays

## Question
Can attacker-paced a HostUpdateNotification for another host's address race concurrent readers of the structures `relayManager.StartRelays` (relay_manager.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `relay_manager.go` -> `relayManager.StartRelays` (declared at relay_manager.go:61)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `relayManager.StartRelays` concurrently, asserting no fatal error.
