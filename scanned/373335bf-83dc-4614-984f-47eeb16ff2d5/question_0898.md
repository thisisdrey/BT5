# Q0898: Signature verification gap for the issuer/CA fingerprint in parseSignature

## Question
Can an unprivileged attacker craft a certificate where the issuer/CA fingerprint is not covered by the bytes actually signature-checked in `parseSignature` (cert/p256/p256.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/p256/p256.go` -> `parseSignature` (declared at cert/p256/p256.go:89)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the issuer/CA fingerprint, and see whether `parseSignature` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the issuer/CA fingerprint on a signed fixture and asserting `parseSignature` returns a verification error.
