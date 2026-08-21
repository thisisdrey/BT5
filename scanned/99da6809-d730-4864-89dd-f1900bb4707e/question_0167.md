# Q0167: Handshake accepted without valid CA chain in ConnectionState.VerifyRelay

## Question
Can an unprivileged attacker complete the path through `ConnectionState.VerifyRelay` (connection_state.go) using a burst of handshakes from one source address and end up in the hostmap as an established peer without presenting a certificate that chains to a trusted CA?

## Target
- File/function: `connection_state.go` -> `ConnectionState.VerifyRelay` (declared at connection_state.go:112)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a burst of handshakes from one source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present a self-signed certificate in the handshake payload and trace whether `ConnectionState.VerifyRelay` reaches the 'established' transition before verification fails.
- Invariant to test: No code path inserts a hostinfo into the active hostmap before certificate verification and the noise handshake both succeed.
- Expected Immunefi impact: Full authentication bypass: an uncertified host gains overlay network access.
- Fast validation: Integration test in the e2e harness handshaking with an untrusted-CA certificate and asserting the responder's hostmap stays empty.
