# Q0943: Signature verification gap for a trailing-byte ASN.1 encoding in certificateV1.MarshalPEM

## Question
Can an unprivileged attacker craft a certificate where a trailing-byte ASN.1 encoding is not covered by the bytes actually signature-checked in `certificateV1.MarshalPEM` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.MarshalPEM` (declared at cert/cert_v1.go:247)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a trailing-byte ASN.1 encoding, and see whether `certificateV1.MarshalPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a trailing-byte ASN.1 encoding on a signed fixture and asserting `certificateV1.MarshalPEM` returns a verification error.
