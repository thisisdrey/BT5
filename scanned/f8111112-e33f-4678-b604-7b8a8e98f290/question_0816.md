# Q0816: Signature verification gap for the Curve field in certificateV2.Marshal

## Question
Can an unprivileged attacker craft a certificate where the Curve field is not covered by the bytes actually signature-checked in `certificateV2.Marshal` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.Marshal` (declared at cert/cert_v2.go:263)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Curve field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Curve field, and see whether `certificateV2.Marshal` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Curve field on a signed fixture and asserting `certificateV2.Marshal` returns a verification error.
