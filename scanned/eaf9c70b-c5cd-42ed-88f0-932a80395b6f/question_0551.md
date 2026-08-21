# Q0551: Signature verification gap for a self-signed certificate in certificateV1.Fingerprint

## Question
Can an unprivileged attacker craft a certificate where a self-signed certificate is not covered by the bytes actually signature-checked in `certificateV1.Fingerprint` (cert/cert_v1.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.Fingerprint` (declared at cert/cert_v1.go:98)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a self-signed certificate, and see whether `certificateV1.Fingerprint` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a self-signed certificate on a signed fixture and asserting `certificateV1.Fingerprint` returns a verification error.
