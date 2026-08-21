# Q0156: Signature verification gap for the NotBefore/NotAfter window in aes256DeriveKey

## Question
Can an unprivileged attacker craft a certificate where the NotBefore/NotAfter window is not covered by the bytes actually signature-checked in `aes256DeriveKey` (cert/crypto.go), so the field is attacker-chosen on an otherwise valid certificate?

## Target
- File/function: `cert/crypto.go` -> `aes256DeriveKey` (declared at cert/crypto.go:111)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Take a legitimately CA-signed certificate, mutate the NotBefore/NotAfter window, and see whether `aes256DeriveKey` still verifies it.
- Invariant to test: The signature verification input covers every field that any authorization decision later reads.
- Expected Immunefi impact: Certificate forgery: attacker grants itself arbitrary groups, networks, or validity while chaining to a real CA.
- Fast validation: Unit test mutating the NotBefore/NotAfter window on a signed fixture and asserting `aes256DeriveKey` returns a verification error.
