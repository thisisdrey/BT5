# Q0636: Signature verification gap for the IsCA flag in Normalize

## Question
Can an unprivileged attacker craft a certificate where the IsCA flag is not covered by the bytes actually signature-checked in `Normalize` (cert/p256/p256.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/p256/p256.go` -> `Normalize` (declared at cert/p256/p256.go:55)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the IsCA flag, and see whether `Normalize` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the IsCA flag on a signed fixture and asserting `Normalize` returns a verification error.
