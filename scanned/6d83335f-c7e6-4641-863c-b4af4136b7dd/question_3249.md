# Q3249: Fingerprint/identity collision in NewCAPool

## Question
Can two distinct certificates produce the same value from `NewCAPool` (cert/ca_pool.go) when the issuer/CA fingerprint differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `NewCAPool` (declared at cert/ca_pool.go:21)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the issuer/CA fingerprint and compare the identifier `NewCAPool` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `NewCAPool` returns different values for any two certificates differing in any field.
