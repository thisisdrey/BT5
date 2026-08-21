# Q3477: Remote list growth/eviction in Punchy.SendPunch

## Question
Can an attacker use a duplicate/looping relay chain to make `Punchy.SendPunch` (punchy.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `punchy.go` -> `Punchy.SendPunch` (declared at punchy.go:167)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a duplicate/looping relay chain; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `Punchy.SendPunch` asserting the bound holds and the live path survives.
