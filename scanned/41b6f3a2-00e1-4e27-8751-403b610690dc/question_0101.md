# Q0101: Unauthenticated remote update via hostnamesResults.Cancel

## Question
Can an unprivileged attacker send an advertised private/loopback remote so `hostnamesResults.Cancel` (remote_list.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `remote_list.go` -> `hostnamesResults.Cancel` (declared at remote_list.go:171)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `hostnamesResults.Cancel` and asserting the peer's remote is unchanged.
