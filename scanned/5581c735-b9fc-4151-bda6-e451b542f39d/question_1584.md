# Q1584: Error message / timing oracle in unmarshalPayloadDetails

## Question
Do the distinct failure paths in `unmarshalPayloadDetails` (handshake/payload.go) for a handshake with a Details/Networks mismatch reveal, by timing or observable response, whether a given identity or index exists on the node?

## Target
- File/function: `handshake/payload.go` -> `unmarshalPayloadDetails` (declared at handshake/payload.go:100)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a Details/Networks mismatch; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Compare responses/latency for a valid-but-unauthorized versus a nonexistent identity.
- Invariant to test: Handshake rejection is indistinguishable across failure causes from the network.
- Expected Immunefi impact: Information disclosure enabling targeted attacks against specific overlay hosts.
- Fast validation: Timing test over many trials asserting the two failure paths in `unmarshalPayloadDetails` are statistically indistinguishable.
