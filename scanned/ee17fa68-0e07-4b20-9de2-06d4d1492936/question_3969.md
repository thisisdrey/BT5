# Q3969: Relay authorization in Punchy.Schedule

## Question
Does `Punchy.Schedule` (punchy.go) verify that the requester is authorized to relay for the named target when handling a punch notification naming an arbitrary target?

## Target
- File/function: `punchy.go` -> `Punchy.Schedule` (declared at punchy.go:142)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Request a relay for a pair the requester has no relationship with and observe whether state is created.
- Invariant to test: Relay state is created only for peers explicitly permitted by configuration and proven by authenticated traffic.
- Expected Immunefi impact: Unauthorized use of a node as a relay, enabling traffic interposition or resource abuse.
- Fast validation: Unit test requesting an unauthorized relay through `Punchy.Schedule` and asserting refusal.
