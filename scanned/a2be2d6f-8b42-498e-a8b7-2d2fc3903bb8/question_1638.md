# Q1638: Error message / timing oracle in subtypeInfoFor

## Question
Do the distinct failure paths in `subtypeInfoFor` (handshake/patterns.go) for a handshake whose remote index collides with a live one reveal, by timing or observable response, whether a given identity or index exists on the node?

## Target
- File/function: `handshake/patterns.go` -> `subtypeInfoFor` (declared at handshake/patterns.go:49)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake whose remote index collides with a live one; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Compare responses/latency for a valid-but-unauthorized versus a nonexistent identity.
- Invariant to test: Handshake rejection is indistinguishable across failure causes from the network.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against specific overlay hosts.
- Fast validation: Timing test over many trials asserting the two failure paths in `subtypeInfoFor` are statistically indistinguishable.
