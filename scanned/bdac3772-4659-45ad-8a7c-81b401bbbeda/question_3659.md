# Q3659: Empty/absent a trailing-byte ASN.1 encoding treated as permissive by NewCAPool

## Question
Does `NewCAPool` (cert/ca_pool.go) treat an absent or empty a trailing-byte ASN.1 encoding as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/ca_pool.go` -> `NewCAPool` (declared at cert/ca_pool.go:21)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a trailing-byte ASN.1 encoding and asserting no traffic is authorized by it.
