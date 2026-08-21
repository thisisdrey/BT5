# Q3091: Empty/absent the Networks field treated as permissive by UnmarshalNebulaEncryptedData

## Question
Does `UnmarshalNebulaEncryptedData` (cert/crypto.go) treat an absent or empty the Networks field as 'unrestricted' rather than 'nothing permitted'?

## Target
- File/function: `cert/crypto.go` -> `UnmarshalNebulaEncryptedData` (declared at cert/crypto.go:195)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Omit the field entirely and observe whether later authorization treats the certificate as unconstrained.
- Invariant to test: Missing constraint fields deny by default; an empty list grants nothing.
- Expected Immunefi impact: Firewall and network-scope bypass by omitting the constraining field from the certificate.
- Fast validation: Unit test verifying a certificate with an empty the Networks field and asserting no traffic is authorized by it.
