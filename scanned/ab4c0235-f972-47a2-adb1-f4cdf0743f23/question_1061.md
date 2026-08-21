# Q1061: Remote list growth/eviction in calculatedRemote.ApplyV6

## Question
Can an attacker use a punch notification naming an arbitrary target to make `calculatedRemote.ApplyV6` (calculated_remote.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.ApplyV6` (declared at calculated_remote.go:59)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `calculatedRemote.ApplyV6` asserting the bound holds and the live path survives.
