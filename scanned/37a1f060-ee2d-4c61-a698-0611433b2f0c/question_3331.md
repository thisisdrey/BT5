# Q3331: Fingerprint/identity collision in CAPool.AddCAFromPEM

## Question
Can two distinct certificates produce the same value from `CAPool.AddCAFromPEM` (cert/ca_pool.go) when a v2 certificate presented where v1 is expected differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/ca_pool.go` -> `CAPool.AddCAFromPEM` (declared at cert/ca_pool.go:86)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a v2 certificate presented where v1 is expected and compare the identifier `CAPool.AddCAFromPEM` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `CAPool.AddCAFromPEM` returns different values for any two certificates differing in any field.
