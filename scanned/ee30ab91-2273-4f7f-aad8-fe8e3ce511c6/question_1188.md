# Q1188: Relay authorization in calculatedRemote.String

## Question
Does `calculatedRemote.String` (calculated_remote.go) verify that the requester is authorized to relay for the named target when handling a HostUpdateNotification for another host's address?

## Target
- File/function: `calculated_remote.go` -> `calculatedRemote.String` (declared at calculated_remote.go:41)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a HostUpdateNotification for another host's address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Request a relay for a pair the requester has no relationship with and observe whether state is created.
- Invariant to test: Relay state is created only for peers explicitly permitted by configuration and proven by authenticated traffic.
- Expected Immunefi impact: Unauthorized use of a node as a relay, enabling traffic interposition or resource abuse.
- Fast validation: Unit test requesting an unauthorized relay through `calculatedRemote.String` and asserting refusal.
