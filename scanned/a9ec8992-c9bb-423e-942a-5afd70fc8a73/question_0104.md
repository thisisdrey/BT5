# Q0104: Signature verification gap for the Groups field in NewErrInvalidCertificateProperties

## Question
Can an unprivileged attacker craft a certificate where the Groups field is not covered by the bytes actually signature-checked in `NewErrInvalidCertificateProperties` (cert/errors.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/errors.go` -> `NewErrInvalidCertificateProperties` (declared at cert/errors.go:46)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Groups field, and see whether `NewErrInvalidCertificateProperties` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Groups field on a signed fixture and asserting `NewErrInvalidCertificateProperties` returns a verification error.
