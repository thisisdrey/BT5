# Q0272: Unauthenticated remote update via newCalculatedRemotesEntryFromConfig

## Question
Can an unprivileged attacker send a punch notification naming an arbitrary target so `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `newCalculatedRemotesEntryFromConfig` and asserting the peer's remote is unchanged.
