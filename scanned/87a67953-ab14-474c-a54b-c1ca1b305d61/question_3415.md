# Q3415: Fingerprint/identity collision in CAPool.VerifyCertificate

## Question
Can two distinct certificates produce the same value from `CAPool.VerifyCertificate` (cert/ca_pool.go) when the Groups field differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.VerifyCertificate` (declared at cert/ca_pool.go:157)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Groups field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the Groups field and compare the identifier `CAPool.VerifyCertificate` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.VerifyCertificate` returns different values for any two certificates differing in any field.
