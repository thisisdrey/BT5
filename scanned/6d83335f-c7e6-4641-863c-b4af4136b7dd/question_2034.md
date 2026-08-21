# Q2034: Relay authorization in relayManager.handleCreateRelayResponse

## Question
Does `relayManager.handleCreateRelayResponse` (relay_manager.go) verify that the requester is authorized to relay for the named target when handling an advertised remote pointing at a third party?

## Target
- File/function: `relay_manager.go` -> `relayManager.handleCreateRelayResponse` (declared at relay_manager.go:344)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an advertised remote pointing at a third party; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Request a relay for a pair the requester has no relationship with and observe whether state is created.
- Invariant to test: Relay state is created only for peers explicitly permitted by configuration and proven by authenticated traffic.
- Expected Immunefi impact: Unauthorized use of a node as a relay, enabling traffic interposition or resource abuse.
- Fast validation: Unit test requesting an unauthorized relay through `relayManager.handleCreateRelayResponse` and asserting refusal.
