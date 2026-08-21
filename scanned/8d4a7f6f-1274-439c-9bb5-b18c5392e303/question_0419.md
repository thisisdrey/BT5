# Q0419: Signature verification gap for an oversized length prefix in certificateV1.MarshalPublicKeyPEM

## Question
Can an unprivileged attacker craft a certificate where an oversized length prefix is not covered by the bytes actually signature-checked in `certificateV1.MarshalPublicKeyPEM` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.MarshalPublicKeyPEM` (declared at cert/cert_v1.go:86)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an oversized length prefix, and see whether `certificateV1.MarshalPublicKeyPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an oversized length prefix on a signed fixture and asserting `certificateV1.MarshalPublicKeyPEM` returns a verification error.
