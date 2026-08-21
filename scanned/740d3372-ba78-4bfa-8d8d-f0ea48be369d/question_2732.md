# Q2732: Expiry window handling in CheckCAConstraints

## Question
Can the NotBefore/NotAfter window make `CheckCAConstraints` (cert/ca_pool.go) treat a certificate as valid outside its NotBefore/NotAfter window, for example through inverted, zero, or overflowing timestamps?

## Target
- File/function: `cert/ca_pool.go` -> `CheckCAConstraints` (declared at cert/ca_pool.go:282)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the NotBefore/NotAfter window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Set NotAfter before NotBefore, or use extreme timestamps, and check the comparison in `CheckCAConstraints`.
- Invariant to test: A certificate is valid only when NotBefore <= now <= NotAfter, with degenerate windows rejected outright.
- Expected Immunefi impact: Use of expired or not-yet-valid credentials to obtain overlay access.
- Fast validation: Table-driven unit test over degenerate time windows asserting `CheckCAConstraints` rejects each.
