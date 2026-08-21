# Q0288: Signature verification gap for a trailing-byte ASN.1 encoding in certificateV2.NotAfter

## Question
Can an unprivileged attacker craft a certificate where a trailing-byte ASN.1 encoding is not covered by the bytes actually signature-checked in `certificateV2.NotAfter` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.NotAfter` (declared at cert/cert_v2.go:105)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a trailing-byte ASN.1 encoding, and see whether `certificateV2.NotAfter` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a trailing-byte ASN.1 encoding on a signed fixture and asserting `certificateV2.NotAfter` returns a verification error.
