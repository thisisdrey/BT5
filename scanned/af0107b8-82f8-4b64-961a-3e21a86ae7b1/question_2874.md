# Q2874: Punch notification abuse in relayManager.HandleControlMsg

## Question
Can an attacker use a HostQueryReply for an unrequested VPN address in `relayManager.HandleControlMsg` (relay_manager.go) to make a node emit packets toward an arbitrary address of the attacker's choosing?

## Target
- File/function: `relay_manager.go` -> `relayManager.HandleControlMsg` (declared at relay_manager.go:298)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a punch notification naming a target address and count the resulting outbound packets.
- Invariant to test: Punch targets are limited to addresses associated with an authenticated peer relationship.
- Expected Immunefi impact: Reflection/amplification abuse of nodes against third-party targets.
- Fast validation: Integration test asserting `relayManager.HandleControlMsg` sends nothing toward an address that has no authenticated relationship.
