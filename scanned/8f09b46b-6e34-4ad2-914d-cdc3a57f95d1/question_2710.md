# Q2710: Remote list growth/eviction in Gateway.BucketUpperBound

## Question
Can an attacker use a HostUpdateNotification for another host's address to make `Gateway.BucketUpperBound` (routing/gateway.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `routing/gateway.go` -> `Gateway.BucketUpperBound` (declared at routing/gateway.go:36)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `Gateway.BucketUpperBound` asserting the bound holds and the live path survives.
