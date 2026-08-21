# Q2820: Concurrent map mutation in newCalculatedRemote

## Question
Can attacker-paced an advertised private/loopback remote race concurrent readers of the structures `newCalculatedRemote` (calculated_remote.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemote` (declared at calculated_remote.go:24)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `newCalculatedRemote` concurrently, asserting no fatal error.
