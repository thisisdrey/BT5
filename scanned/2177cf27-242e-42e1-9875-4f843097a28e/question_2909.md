# Q2909: Concurrent map mutation in calculatedRemote.ApplyV4

## Question
Can attacker-paced a relay request for a host it does not own race concurrent readers of the structures `calculatedRemote.ApplyV4` (calculated_remote.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV4` (declared at calculated_remote.go:45)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `calculatedRemote.ApplyV4` concurrently, asserting no fatal error.
