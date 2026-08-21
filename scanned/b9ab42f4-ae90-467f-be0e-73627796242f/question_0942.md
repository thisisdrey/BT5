# Q0942: Empty/absent a v2 certificate presented where v1 is expected treated as permissive by CalculateAlternateFingerprint

## Question
Does `CalculateAlternateFingerprint` (cert/cert.go) treat an absent or empty a v2 certificate presented where v1 is expected as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/cert.go` -> `CalculateAlternateFingerprint` (declared at cert/cert.go:163)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a v2 certificate presented where v1 is expected and asserting no traffic is authorized by it.
