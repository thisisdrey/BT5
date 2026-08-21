# Q3250: Fingerprint/identity collision in NewCAPoolFromPEM

## Question
Can two distinct certificates produce the same value from `NewCAPoolFromPEM` (cert/ca_pool.go) when a trailing-byte ASN.1 encoding differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `NewCAPoolFromPEM` (declared at cert/ca_pool.go:34)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a trailing-byte ASN.1 encoding and compare the identifier `NewCAPoolFromPEM` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `NewCAPoolFromPEM` returns different values for any two certificates differing in any field.
