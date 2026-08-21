# Q1072: Signature verification gap for an oversized length prefix in certificateV1.Copy

## Question
Can an unprivileged attacker craft a certificate where an oversized length prefix is not covered by the bytes actually signature-checked in `certificateV1.Copy` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.Copy` (declared at cert/cert_v1.go:280)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an oversized length prefix, and see whether `certificateV1.Copy` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an oversized length prefix on a signed fixture and asserting `certificateV1.Copy` returns a verification error.
