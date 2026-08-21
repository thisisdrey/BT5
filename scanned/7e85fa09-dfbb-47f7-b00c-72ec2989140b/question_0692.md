# Q0692: Expiry window handling in TBSCertificate.SignWith

## Question
Can the Curve field make `TBSCertificate.SignWith` (cert/sign.go) treat a certificate as valid outside its NotBefore/NotAfter window, for example through inverted, zero, or overflowing timestamps?

## Target
- File/function: `cert/sign.go` -> `TBSCertificate.SignWith` (declared at cert/sign.go:77)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Curve field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Set NotAfter before NotBefore, or use extreme timestamps, and check the comparison in `TBSCertificate.SignWith`.
- Invariant to test: A certificate is valid only when NotBefore <= now <= NotAfter, with degenerate windows rejected outright.
- Expected Immunefi impact: Use of expired or not-yet-valid credentials to obtain overlay access.
- Fast validation: Table-driven unit test over degenerate time windows asserting `TBSCertificate.SignWith` rejects each.
