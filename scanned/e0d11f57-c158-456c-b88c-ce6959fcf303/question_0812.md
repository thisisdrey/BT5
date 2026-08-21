# Q0812: Signature verification gap for the IsCA flag in certificateV1.String

## Question
Can an unprivileged attacker craft a certificate where the IsCA flag is not covered by the bytes actually signature-checked in `certificateV1.String` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.String` (declared at cert/cert_v1.go:219)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the IsCA flag, and see whether `certificateV1.String` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the IsCA flag on a signed fixture and asserting `certificateV1.String` returns a verification error.
