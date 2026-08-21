# Q0839: Curve/pattern downgrade in NewCredential

## Question
Can an attacker present a handshake reusing a prior ephemeral key to make `NewCredential` (handshake/credential.go) negotiate a weaker curve, cipher, or noise pattern than both sides support?

## Target
- File/function: `handshake/credential.go` -> `NewCredential` (declared at handshake/credential.go:23)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise the weakest supported parameters and check whether `NewCredential` accepts without verifying against the local policy.
- Invariant to test: Negotiated parameters are validated against local configuration, never taken solely from the peer's advertisement.
- Expected Immunefi impact: Cryptographic downgrade weakening confidentiality of tunnel traffic.
- Fast validation: Unit test on `NewCredential` asserting a downgrade advertisement is rejected when the local config requires the stronger parameter.
