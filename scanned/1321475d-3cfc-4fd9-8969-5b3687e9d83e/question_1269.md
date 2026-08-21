# Q1269: Third-party address advertisement in relayManager.handleCreateRelayRequest

## Question
Can an attacker advertise an advertised private/loopback remote through `relayManager.handleCreateRelayRequest` (relay_manager.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayRequest` (declared at relay_manager.go:426)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised private/loopback remote; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `relayManager.handleCreateRelayRequest` never emits packets to an address advertised for a foreign identity.
