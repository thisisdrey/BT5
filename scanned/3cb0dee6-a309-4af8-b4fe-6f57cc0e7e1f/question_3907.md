# Q3907: Empty/absent the IsCA flag treated as permissive by CAPool.GetCAForCert

## Question
Does `CAPool.GetCAForCert` (cert/ca_pool.go) treat an absent or empty the IsCA flag as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.GetCAForCert` (declared at cert/ca_pool.go:254)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty the IsCA flag and asserting no traffic is authorized by it.
