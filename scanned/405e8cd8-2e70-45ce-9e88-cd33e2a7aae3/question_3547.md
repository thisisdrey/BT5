# Q3547: Remote list growth/eviction in RemoteList.unlockedSetHostnamesResults

## Question
Can an attacker use an oversized remote list to make `RemoteList.unlockedSetHostnamesResults` (remote_list.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `remote_list.go` -> `RemoteList.unlockedSetHostnamesResults` (declared at remote_list.go:236)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `RemoteList.unlockedSetHostnamesResults` asserting the bound holds and the live path survives.
