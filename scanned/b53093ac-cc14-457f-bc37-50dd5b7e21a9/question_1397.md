# Q1397: Signature verification gap for an oversized length prefix in UnmarshalPrivateKeyFromPEM

## Question
Can an unprivileged attacker craft a certificate where an oversized length prefix is not covered by the bytes actually signature-checked in `UnmarshalPrivateKeyFromPEM` (cert/pem.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/pem.go` -> `UnmarshalPrivateKeyFromPEM` (declared at cert/pem.go:229)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate an oversized length prefix, and see whether `UnmarshalPrivateKeyFromPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating an oversized length prefix on a signed fixture and asserting `UnmarshalPrivateKeyFromPEM` returns a verification error.
