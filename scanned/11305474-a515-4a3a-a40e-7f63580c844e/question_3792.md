# Q3792: Remote list growth/eviction in RemoteList.BlockRemote

## Question
Can an attacker use a HostUpdateNotification for another host's address to make `RemoteList.BlockRemote` (remote_list.go) retain an unbounded number of candidate remotes, or evict the correct one?

## Target
- File/function: `remote_list.go` -> `RemoteList.BlockRemote` (declared at remote_list.go:378)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise many distinct remotes for one identity and measure retention and selection.
- Invariant to test: Candidate remotes per host are bounded and the verified working path is never evicted by unverified candidates.
- Expected Immunefi impact: Memory growth and tunnel disruption for a peer chosen by the attacker.
- Fast validation: Unit test advertising N remotes through `RemoteList.BlockRemote` asserting the bound holds and the live path survives.
