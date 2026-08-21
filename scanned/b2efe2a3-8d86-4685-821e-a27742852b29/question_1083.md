# Q1083: Fingerprint/identity collision in comparePrefix

## Question
Can two distinct certificates produce the same value from `comparePrefix` (cert/sign.go) when a v1 certificate presented where v2 is expected differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/sign.go` -> `comparePrefix` (declared at cert/sign.go:151)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a v1 certificate presented where v2 is expected and compare the identifier `comparePrefix` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `comparePrefix` returns different values for any two certificates differing in any field.
