# Q3414: Fingerprint/identity collision in CAPool.IsBlocklisted

## Question
Can two distinct certificates produce the same value from `CAPool.IsBlocklisted` (cert/ca_pool.go) when a self-signed certificate differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.IsBlocklisted` (declared at cert/ca_pool.go:146)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a self-signed certificate and compare the identifier `CAPool.IsBlocklisted` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.IsBlocklisted` returns different values for any two certificates differing in any field.
