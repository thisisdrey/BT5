# Q0552: Signature verification gap for the Groups field in certificateV1.CheckSignature

## Question
Can an unprivileged attacker craft a certificate where the Groups field is not covered by the bytes actually signature-checked in `certificateV1.CheckSignature` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.CheckSignature` (declared at cert/cert_v1.go:108)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Groups field, and see whether `certificateV1.CheckSignature` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Groups field on a signed fixture and asserting `certificateV1.CheckSignature` returns a verification error.
