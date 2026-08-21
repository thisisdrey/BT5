# Q1326: Empty/absent an oversized length prefix treated as permissive by findDuplicatePrefix

## Question
Does `findDuplicatePrefix` (cert/sign.go) treat an absent or empty an oversized length prefix as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/sign.go` -> `findDuplicatePrefix` (declared at cert/sign.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty an oversized length prefix and asserting no traffic is authorized by it.
