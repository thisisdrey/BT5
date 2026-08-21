# Q0672: Empty/absent a v1 certificate presented where v2 is expected treated as permissive by readOptionalASN1Byte

## Question
Does `readOptionalASN1Byte` (cert/asn1.go) treat an absent or empty a v1 certificate presented where v2 is expected as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Byte` (declared at cert/asn1.go:33)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a v1 certificate presented where v2 is expected and asserting no traffic is authorized by it.
