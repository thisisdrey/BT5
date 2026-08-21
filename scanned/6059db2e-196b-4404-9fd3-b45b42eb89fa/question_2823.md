# Q2823: IsCA / chain-depth confusion in NewCAPoolFromPEMReader

## Question
Can an attacker use a trailing-byte ASN.1 encoding so `NewCAPoolFromPEMReader` (cert/ca_pool.go) treats a leaf certificate as a signing CA, or accepts a chain deeper or looped beyond what is intended?

## Target
- File/function: `cert/ca_pool.go` -> `NewCAPoolFromPEMReader` (declared at cert/ca_pool.go:40)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Present a leaf marked or interpretable as a CA and attempt to have it validate a further certificate.
- Invariant to test: Only certificates explicitly marked IsCA and present in the trusted pool can validate others; chains are depth-bounded and loop-free.
- Expected Immunefi impact: Full trust bypass: the attacker becomes an issuer and mints arbitrary identities.
- Fast validation: Unit test attempting chain validation through a leaf via `NewCAPoolFromPEMReader` and asserting rejection.
