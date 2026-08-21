# Q3061: Remote list growth/eviction in CalculateBucketsForGateways

## Question
Can an attacker use a preferred-ranges entry to make `CalculateBucketsForGateways` (routing/gateway.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `routing/gateway.go` -> `CalculateBucketsForGateways` (declared at routing/gateway.go:57)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `CalculateBucketsForGateways` asserting the bound holds and the live path survives.
