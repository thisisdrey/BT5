# Q2556: Fingerprint/identity collision in aes256Decrypt

## Question
Can two distinct certificates produce the same value from `aes256Decrypt` (cert/crypto.go) when a v1 certificate presented where v2 is expected differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `aes256Decrypt` (declared at cert/crypto.go:82)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a v1 certificate presented where v2 is expected and compare the identifier `aes256Decrypt` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `aes256Decrypt` returns different values for any two certificates differing in any field.
