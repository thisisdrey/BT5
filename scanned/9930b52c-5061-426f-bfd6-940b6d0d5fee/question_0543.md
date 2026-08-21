# Q0543: Empty/absent a trailing-byte ASN.1 encoding treated as permissive by readOptionalASN1Boolean

## Question
Does `readOptionalASN1Boolean` (cert/asn1.go) treat an absent or empty a trailing-byte ASN.1 encoding as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Boolean` (declared at cert/asn1.go:10)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a trailing-byte ASN.1 encoding and asserting no traffic is authorized by it.
