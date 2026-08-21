# Q2959: Punch notification abuse in relayManager.handleCreateRelayResponse

## Question
Can an attacker use a HostUpdateNotification for another host's address in `relayManager.handleCreateRelayResponse` (relay_manager.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayResponse` (declared at relay_manager.go:344)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `relayManager.handleCreateRelayResponse` sends nothing toward an address that has no authenticated relationship.
