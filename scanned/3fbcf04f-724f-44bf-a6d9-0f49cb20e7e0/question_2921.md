# Q2921: Empty/absent a v2 certificate presented where v1 is expected treated as permissive by aes256Decrypt

## Question
Does `aes256Decrypt` (cert/crypto.go) treat an absent or empty a v2 certificate presented where v1 is expected as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/crypto.go` -> `aes256Decrypt` (declared at cert/crypto.go:82)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty a v2 certificate presented where v1 is expected and asserting no traffic is authorized by it.
