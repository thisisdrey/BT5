# Q2184: Curve/pattern downgrade in newConnectionStateFromResult

## Question
Can an attacker present a handshake reusing a prior ephemeral key to make `newConnectionStateFromResult` (connection_state.go) negotiate a weaker curve, cipher, or noise pattern than both sides support?

## Target
- File/function: `connection_state.go` -> `newConnectionStateFromResult` (declared at connection_state.go:45)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise the weakest supported parameters and check whether `newConnectionStateFromResult` accepts without verifying against the local policy.
- Invariant to test: Negotiated parameters are validated against local configuration, never taken solely from the peer's advertisement.
- Expected Immunefi impact: Cryptographic downgrade weakening confidentiality of tunnel traffic.
- Fast validation: Unit test on `newConnectionStateFromResult` asserting a downgrade advertisement is rejected when the local config requires the stronger parameter.
