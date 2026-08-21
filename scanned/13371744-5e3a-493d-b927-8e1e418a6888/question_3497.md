# Q3497: Fingerprint/identity collision in CAPool.GetCAForCert

## Question
Can two distinct certificates produce the same value from `CAPool.GetCAForCert` (cert/ca_pool.go) when the NotBefore/NotAfter window differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.GetCAForCert` (declared at cert/ca_pool.go:254)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the NotBefore/NotAfter window and compare the identifier `CAPool.GetCAForCert` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.GetCAForCert` returns different values for any two certificates differing in any field.
