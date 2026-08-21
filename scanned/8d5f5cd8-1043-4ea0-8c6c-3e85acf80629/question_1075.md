# Q1075: Signature verification gap for an oversized length prefix in certificateV2.fromTBSCertificate

## Question
Can an unprivileged attacker craft a certificate where an oversized length prefix is not covered by the bytes actually signature-checked in `certificateV2.fromTBSCertificate` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.fromTBSCertificate` (declared at cert/cert_v2.go:375)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an oversized length prefix, and see whether `certificateV2.fromTBSCertificate` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an oversized length prefix on a signed fixture and asserting `certificateV2.fromTBSCertificate` returns a verification error.
