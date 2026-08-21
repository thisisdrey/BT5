# Q0461: Unauthenticated remote update via RelayState.CompleteRelayByIdx

## Question
Can an unprivileged attacker send a spoofed UDP source address so `RelayState.CompleteRelayByIdx` (hostmap.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `hostmap.go` -> `RelayState.CompleteRelayByIdx` (declared at hostmap.go:192)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `RelayState.CompleteRelayByIdx` and asserting the peer's remote is unchanged.
