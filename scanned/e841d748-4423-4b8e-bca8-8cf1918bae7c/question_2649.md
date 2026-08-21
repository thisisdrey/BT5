# Q2649: Fingerprint/identity collision in deriveKey

## Question
Can two distinct certificates produce the same value from `deriveKey` (cert/crypto.go) when an oversized length prefix differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `deriveKey` (declared at cert/crypto.go:129)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in an oversized length prefix and compare the identifier `deriveKey` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `deriveKey` returns different values for any two certificates differing in any field.
