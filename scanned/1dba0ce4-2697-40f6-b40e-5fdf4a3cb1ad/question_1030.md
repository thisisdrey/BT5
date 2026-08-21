# Q1030: Signature verification gap for a trailing-byte ASN.1 encoding in UnmarshalSigningPublicKeyFromPEM

## Question
Can an unprivileged attacker craft a certificate where a trailing-byte ASN.1 encoding is not covered by the bytes actually signature-checked in `UnmarshalSigningPublicKeyFromPEM` (cert/pem.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/pem.go` -> `UnmarshalSigningPublicKeyFromPEM` (declared at cert/pem.go:181)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate a trailing-byte ASN.1 encoding, and see whether `UnmarshalSigningPublicKeyFromPEM` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating a trailing-byte ASN.1 encoding on a signed fixture and asserting `UnmarshalSigningPublicKeyFromPEM` returns a verification error.
