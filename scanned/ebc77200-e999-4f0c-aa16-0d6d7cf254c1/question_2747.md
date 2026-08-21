# Q2747: Error message / timing oracle in ConnectionState.MarshalJSON

## Question
Do the distinct failure paths in `ConnectionState.MarshalJSON` (connection_state.go) for a handshake carrying an empty certificate field reveal, by timing or observable response, whether a given identity or index exists on the node?

## Target
- File/function: `connection_state.go` -> `ConnectionState.MarshalJSON` (declared at connection_state.go:66)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake carrying an empty certificate field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Compare responses/latency for a valid-but-unauthorized versus a nonexistent identity.
- Invariant to test: Handshake rejection is indistinguishable across failure causes from the network.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against specific overlay hosts.
- Fast validation: Timing test over many trials asserting the two failure paths in `ConnectionState.MarshalJSON` are statistically indistinguishable.
