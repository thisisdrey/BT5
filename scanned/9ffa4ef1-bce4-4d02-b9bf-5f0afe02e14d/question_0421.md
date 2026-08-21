# Q0421: Signature verification gap for a v2 certificate presented where v1 is expected in certificateV2.PublicKey

## Question
Can an unprivileged attacker craft a certificate where a v2 certificate presented where v1 is expected is not covered by the bytes actually signature-checked in `certificateV2.PublicKey` (cert/cert_v2.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v2.go` -> `certificateV2.PublicKey` (declared at cert/cert_v2.go:113)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a v2 certificate presented where v1 is expected, and see whether `certificateV2.PublicKey` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a v2 certificate presented where v1 is expected on a signed fixture and asserting `certificateV2.PublicKey` returns a verification error.
