# Q0420: Signature verification gap for a duplicated or out-of-order ASN.1 field in certificateV1.Signature

## Question
Can an unprivileged attacker craft a certificate where a duplicated or out-of-order ASN.1 field is not covered by the bytes actually signature-checked in `certificateV1.Signature` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.Signature` (declared at cert/cert_v1.go:90)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a duplicated or out-of-order ASN.1 field, and see whether `certificateV1.Signature` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a duplicated or out-of-order ASN.1 field on a signed fixture and asserting `certificateV1.Signature` returns a verification error.
