# Q0367: Unauthenticated remote update via relayManager.handleCreateRelayResponse

## Question
Can an unprivileged attacker send a duplicate/looping relay chain so `relayManager.handleCreateRelayResponse` (relay_manager.go) changes the underlay address recorded for an established peer?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayResponse` (declared at relay_manager.go:344)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a duplicate/looping relay chain; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send the address-bearing packet type for a live session from an unrelated source address.
- Invariant to test: A peer's recorded remote changes only after authenticated traffic from that peer proves the new path.
- Expected Immunefi impact: Traffic redirection/blackholing of an established tunnel by an off-path attacker.
- Fast validation: Integration test injecting an unauthenticated update through `relayManager.handleCreateRelayResponse` and asserting the peer's remote is unchanged.
