# Q2910: Concurrent map mutation in calculatedRemote.ApplyV6

## Question
Can attacker-paced a HostQueryReply for an unrequested VPN address race concurrent readers of the structures `calculatedRemote.ApplyV6` (calculated_remote.go) mutates, causing a fatal concurrent map access?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV6` (declared at calculated_remote.go:59)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Drive inserts and lookups in parallel at attacker-controlled rates.
- Invariant to test: All hostmap/remote-list access is properly synchronized under concurrent packet load.
- Expected Immunefi impact: Remote node crash from unauthenticated traffic patterns.
- Fast validation: `-race` stress test driving `calculatedRemote.ApplyV6` concurrently, asserting no fatal error.
