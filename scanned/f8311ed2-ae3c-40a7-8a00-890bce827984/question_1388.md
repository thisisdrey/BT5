# Q1388: Remote list growth/eviction in NewRelayManager

## Question
Can an attacker use a relay request for a host it does not own to make `NewRelayManager` (relay_manager.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `relay_manager.go` -> `NewRelayManager` (declared at relay_manager.go:25)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `NewRelayManager` asserting the bound holds and the live path survives.
