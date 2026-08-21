# Q3988: Empty/absent the issuer/CA fingerprint treated as permissive by CheckCAConstraints

## Question
Does `CheckCAConstraints` (cert/ca_pool.go) treat an absent or empty the issuer/CA fingerprint as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/ca_pool.go` -> `CheckCAConstraints` (declared at cert/ca_pool.go:282)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty the issuer/CA fingerprint and asserting no traffic is authorized by it.
