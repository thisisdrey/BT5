# Q1886: Expiry window handling in aes256DeriveKey

## Question
Can a trailing-byte ASN.1 encoding make `aes256DeriveKey` (cert/crypto.go) treat a certificate as valid outside its NotBefore/NotAfter window, for example through inverted, zero, or overflowing timestamps?

## Target
- File/function: `cert/crypto.go` -> `aes256DeriveKey` (declared at cert/crypto.go:111)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Set NotAfter before NotBefore, or use extreme timestamps, and check the comparison in `aes256DeriveKey`.
- Invariant to test: A certificate is valid only when NotBefore <= now <= NotAfter, with degenerate windows rejected outright.
- Expected Immunefi impact: Use of expired or not-yet-valid credentials to obtain overlay access.
- Fast validation: Table-driven unit test over degenerate time windows asserting `aes256DeriveKey` rejects each.
