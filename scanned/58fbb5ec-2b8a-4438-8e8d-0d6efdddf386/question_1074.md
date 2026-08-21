# Q1074: Signature verification gap for an empty Networks list in certificateV1.validate

## Question
Can an unprivileged attacker craft a certificate where an empty Networks list is not covered by the bytes actually signature-checked in `certificateV1.validate` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.validate` (declared at cert/cert_v1.go:332)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an empty Networks list, and see whether `certificateV1.validate` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an empty Networks list on a signed fixture and asserting `certificateV1.validate` returns a verification error.
