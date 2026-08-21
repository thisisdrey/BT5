# Q0762: Unauthenticated remote update via RemoteList.ResetBlockedRemotes

## Question
Can an unprivileged attacker send a HostUpdateNotification for another host's address so `RemoteList.ResetBlockedRemotes` (remote_list.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `remote_list.go` -> `RemoteList.ResetBlockedRemotes` (declared at remote_list.go:420)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `RemoteList.ResetBlockedRemotes` and asserting the peer's remote is unchanged.
