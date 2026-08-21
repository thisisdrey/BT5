# Q1186: Remote list growth/eviction in newCalculatedRemotesEntryFromConfig

## Question
Can an attacker use a preferred-ranges entry to make `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `newCalculatedRemotesEntryFromConfig` asserting the bound holds and the live path survives.
