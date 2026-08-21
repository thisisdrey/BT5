# Q0238: Signature verification gap for the Networks field in ErrInvalidCertificateProperties.Error

## Question
Can an unprivileged attacker craft a certificate where the Networks field is not covered by the bytes actually signature-checked in `ErrInvalidCertificateProperties.Error` (cert/errors.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/errors.go` -> `ErrInvalidCertificateProperties.Error` (declared at cert/errors.go:50)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Networks field, and see whether `ErrInvalidCertificateProperties.Error` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Networks field on a signed fixture and asserting `ErrInvalidCertificateProperties.Error` returns a verification error.
