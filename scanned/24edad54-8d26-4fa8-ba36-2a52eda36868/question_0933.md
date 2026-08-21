# Q0933: Remote list growth/eviction in calculatedRemote.ApplyV4

## Question
Can an attacker use a HostUpdateNotification for another host's address to make `calculatedRemote.ApplyV4` (calculated_remote.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV4` (declared at calculated_remote.go:45)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `calculatedRemote.ApplyV4` asserting the bound holds and the live path survives.
