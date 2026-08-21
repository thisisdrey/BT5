# Q3496: Fingerprint/identity collision in CAPool.verify

## Question
Can two distinct certificates produce the same value from `CAPool.verify` (cert/ca_pool.go) when the UnsafeNetworks field differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.verify` (declared at cert/ca_pool.go:210)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the UnsafeNetworks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the UnsafeNetworks field and compare the identifier `CAPool.verify` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.verify` returns different values for any two certificates differing in any field.
