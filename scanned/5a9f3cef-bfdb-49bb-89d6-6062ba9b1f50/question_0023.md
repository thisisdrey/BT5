# Q0023: Signature verification gap for the Networks field in aes256Encrypt

## Question
Can an unprivileged attacker craft a certificate where the Networks field is not covered by the bytes actually signature-checked in `aes256Encrypt` (cert/crypto.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/crypto.go` -> `aes256Encrypt` (declared at cert/crypto.go:47)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the Networks field, and see whether `aes256Encrypt` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the Networks field on a signed fixture and asserting `aes256Encrypt` returns a verification error.
