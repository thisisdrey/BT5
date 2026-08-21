# Q2782: Punch notification abuse in relayManager.GetAmRelay

## Question
Can an attacker use a preferred-ranges entry in `relayManager.GetAmRelay` (relay_manager.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `relay_manager.go` -> `relayManager.GetAmRelay` (declared at relay_manager.go:49)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a preferred-ranges entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `relayManager.GetAmRelay` sends nothing toward an address that has no authenticated relationship.
