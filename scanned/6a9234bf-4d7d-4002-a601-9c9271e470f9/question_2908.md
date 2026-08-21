# Q2908: Concurrent map mutation in calculatedRemote.String

## Question
Can attacker-paced an advertised remote pointing at a third party race concurrent readers of the structures `calculatedRemote.String` (calculated_remote.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.String` (declared at calculated_remote.go:41)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised remote pointing at a third party; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `calculatedRemote.String` concurrently, asserting no fatal error.
