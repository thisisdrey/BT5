# Q1223: Curve/pattern downgrade in UnmarshalPayload

## Question
Can an attacker present a replayed stage-1 handshake to make `UnmarshalPayload` (handshake/payload.go) negotiate a weaker curve, cipher, or noise pattern than both sides support?

## Target
- File/function: `handshake/payload.go` -> `UnmarshalPayload` (declared at handshake/payload.go:68)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a replayed stage-1 handshake; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise the weakest supported parameters and check whether `UnmarshalPayload` accepts without verifying against the local policy.
- Invariant to test: Negotiated parameters are validated against local configuration, never taken solely from the peer's advertisement.
- Expected Immunefi impact: Cryptographic downgrade weakening confidentiality of tunnel traffic.
- Fast validation: Unit test on `UnmarshalPayload` asserting a downgrade advertisement is rejected when the local config requires the stronger parameter.
