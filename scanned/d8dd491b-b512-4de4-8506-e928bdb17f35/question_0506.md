# Q0506: Signature verification gap for the NotBefore/NotAfter window in marshalCertPublicKeyToPEM

## Question
Can an unprivileged attacker craft a certificate where the NotBefore/NotAfter window is not covered by the bytes actually signature-checked in `marshalCertPublicKeyToPEM` (cert/pem.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/pem.go` -> `marshalCertPublicKeyToPEM` (declared at cert/pem.go:117)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the NotBefore/NotAfter window, and see whether `marshalCertPublicKeyToPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the NotBefore/NotAfter window on a signed fixture and asserting `marshalCertPublicKeyToPEM` returns a verification error.
