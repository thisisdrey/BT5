# Q1278: Curve/pattern downgrade in subtypeInfoFor

## Question
Can an attacker present a handshake reusing a prior ephemeral key to make `subtypeInfoFor` (handshake/patterns.go) negotiate a weaker curve, cipher, or noise pattern than both sides support?

## Target
- File/function: `handshake/patterns.go` -> `subtypeInfoFor` (declared at handshake/patterns.go:49)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise the weakest supported parameters and check whether `subtypeInfoFor` accepts without verifying against the local policy.
- Invariant to test: Negotiated parameters are validated against local configuration, never taken solely from the peer's advertisement.
- Expected Immunefi impact: Cryptographic downgrade weakening confidentiality of tunnel traffic.
- Fast validation: Unit test on `subtypeInfoFor` asserting a downgrade advertisement is rejected when the local config requires the stronger parameter.
