# Q2740: Fingerprint/identity collision in splitNonceCiphertext

## Question
Can two distinct certificates produce the same value from `splitNonceCiphertext` (cert/crypto.go) when an empty Networks list differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `splitNonceCiphertext` (declared at cert/crypto.go:151)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in an empty Networks list and compare the identifier `splitNonceCiphertext` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `splitNonceCiphertext` returns different values for any two certificates differing in any field.
