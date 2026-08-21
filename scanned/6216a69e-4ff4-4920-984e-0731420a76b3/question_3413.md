# Q3413: Fingerprint/identity collision in CAPool.ResetCertBlocklist

## Question
Can two distinct certificates produce the same value from `CAPool.ResetCertBlocklist` (cert/ca_pool.go) when an empty Networks list differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.ResetCertBlocklist` (declared at cert/ca_pool.go:140)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an empty Networks list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in an empty Networks list and compare the identifier `CAPool.ResetCertBlocklist` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.ResetCertBlocklist` returns different values for any two certificates differing in any field.
