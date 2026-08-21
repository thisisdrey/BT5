# Q2891: Remote list growth/eviction in Gateway.String

## Question
Can an attacker use an oversized remote list to make `Gateway.String` (routing/gateway.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `routing/gateway.go` -> `Gateway.String` (declared at routing/gateway.go:44)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `Gateway.String` asserting the bound holds and the live path survives.
