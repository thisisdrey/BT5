# Q3577: Fingerprint/identity collision in CAPool.GetFingerprints

## Question
Can two distinct certificates produce the same value from `CAPool.GetFingerprints` (cert/ca_pool.go) when the IsCA flag differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.GetFingerprints` (declared at cert/ca_pool.go:269)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the IsCA flag; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the IsCA flag and compare the identifier `CAPool.GetFingerprints` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.GetFingerprints` returns different values for any two certificates differing in any field.
