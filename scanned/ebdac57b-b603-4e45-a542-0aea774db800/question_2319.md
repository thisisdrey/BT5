# Q2319: Relay loop/chain amplification via relayManager.handleCreateRelayRequest

## Question
Can an attacker construct a HostQueryReply for an unrequested VPN address so `relayManager.handleCreateRelayRequest` (relay_manager.go) forwards a packet back into the relay path, creating a loop or multiplying traffic?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayRequest` (declared at relay_manager.go:426)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostQueryReply for an unrequested VPN address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Build a relay chain whose next hop points back at a previous hop.
- Invariant to test: Relay forwarding is depth-limited and never returns a packet to the hop it came from.
- Expected Immunefi impact: Traffic amplification and CPU/bandwidth exhaustion across multiple nodes from one packet.
- Fast validation: Unit test with a looping chain asserting `relayManager.handleCreateRelayRequest` drops after one hop.
