# Q2555: Fingerprint/identity collision in aes256Encrypt

## Question
Can two distinct certificates produce the same value from `aes256Encrypt` (cert/crypto.go) when a trailing-byte ASN.1 encoding differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `aes256Encrypt` (declared at cert/crypto.go:47)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a trailing-byte ASN.1 encoding and compare the identifier `aes256Encrypt` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `aes256Encrypt` returns different values for any two certificates differing in any field.
