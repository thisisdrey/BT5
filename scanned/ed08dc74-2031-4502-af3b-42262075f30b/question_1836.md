# Q1836: Relay authorization in relayManager.GetUseRelays

## Question
Does `relayManager.GetUseRelays` (relay_manager.go) verify that the requester is authorized to relay for the named target when handling an oversized remote list?

## Target
- File/function: `relay_manager.go` -> `relayManager.GetUseRelays` (declared at relay_manager.go:53)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Request a relay for a pair the requester has no relationship with and observe whether state is created.
- Invariant to test: Relay state is created only for peers explicitly permitted by configuration and proven by authenticated traffic.
- Expected Immunefi impact: Unauthorized use of a node as a relay, enabling traffic interposition or resource abuse.
- Fast validation: Unit test requesting an unauthorized relay through `relayManager.GetUseRelays` and asserting refusal.
