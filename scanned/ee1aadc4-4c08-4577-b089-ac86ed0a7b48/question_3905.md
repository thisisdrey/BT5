# Q3905: Empty/absent the UnsafeNetworks field treated as permissive by CAPool.VerifyCachedCertificate

## Question
Does `CAPool.VerifyCachedCertificate` (cert/ca_pool.go) treat an absent or empty the UnsafeNetworks field as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.VerifyCachedCertificate` (declared at cert/ca_pool.go:200)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the UnsafeNetworks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty the UnsafeNetworks field and asserting no traffic is authorized by it.
