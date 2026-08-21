# Q0758: Handshake accepted without valid CA chain in loadCAPoolFromConfig

## Question
Can an unprivileged attacker complete the path through `loadCAPoolFromConfig` (pki.go) using a handshake with a mismatched noise pattern/curve and end up in the hostmap as an established peer without presenting a certificate that chains to a trusted CA?

## Target
- File/function: `pki.go` -> `loadCAPoolFromConfig` (declared at pki.go:525)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake with a mismatched noise pattern/curve; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present a self-signed certificate in the handshake payload and trace whether `loadCAPoolFromConfig` reaches the 'established' transition before verification fails.
- Invariant to test: No code path inserts a hostinfo into the active hostmap before certificate verification and the noise handshake both succeed.
- Expected Immunefi impact: Full authentication bypass: an uncertified host gains overlay network access.
- Fast validation: Integration test in the e2e harness handshaking with an untrusted-CA certificate and asserting the responder's hostmap stays empty.
