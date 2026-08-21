# Q0813: Signature verification gap for the Curve field in certificateV1.MarshalForHandshakes

## Question
Can an unprivileged attacker craft a certificate where the Curve field is not covered by the bytes actually signature-checked in `certificateV1.MarshalForHandshakes` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.MarshalForHandshakes` (declared at cert/cert_v1.go:227)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Curve field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Curve field, and see whether `certificateV1.MarshalForHandshakes` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Curve field on a signed fixture and asserting `certificateV1.MarshalForHandshakes` returns a verification error.
