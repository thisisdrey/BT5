# Q2994: Concurrent map mutation in newCalculatedRemotesListFromConfig

## Question
Can attacker-paced a punch notification naming an arbitrary target race concurrent readers of the structures `newCalculatedRemotesListFromConfig` (calculated_remote.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesListFromConfig` (declared at calculated_remote.go:108)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `newCalculatedRemotesListFromConfig` concurrently, asserting no fatal error.
