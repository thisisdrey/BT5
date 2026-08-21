# Q2650: Fingerprint/identity collision in joinNonceCiphertext

## Question
Can two distinct certificates produce the same value from `joinNonceCiphertext` (cert/crypto.go) when a duplicated or out-of-order ASN.1 field differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `joinNonceCiphertext` (declared at cert/crypto.go:146)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a duplicated or out-of-order ASN.1 field and compare the identifier `joinNonceCiphertext` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `joinNonceCiphertext` returns different values for any two certificates differing in any field.
