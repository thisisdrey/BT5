# Q3823: Empty/absent a self-signed certificate treated as permissive by CAPool.ResetCertBlocklist

## Question
Does `CAPool.ResetCertBlocklist` (cert/ca_pool.go) treat an absent or empty a self-signed certificate as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.ResetCertBlocklist` (declared at cert/ca_pool.go:140)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a self-signed certificate and asserting no traffic is authorized by it.
