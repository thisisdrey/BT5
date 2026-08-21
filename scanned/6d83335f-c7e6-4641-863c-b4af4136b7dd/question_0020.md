# Q0020: Signature verification gap for the Networks field in certificateV2.Curve

## Question
Can an unprivileged attacker craft a certificate where the Networks field is not covered by the bytes actually signature-checked in `certificateV2.Curve` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.Curve` (declared at cert/cert_v2.go:81)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Networks field, and see whether `certificateV2.Curve` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Networks field on a signed fixture and asserting `certificateV2.Curve` returns a verification error.
