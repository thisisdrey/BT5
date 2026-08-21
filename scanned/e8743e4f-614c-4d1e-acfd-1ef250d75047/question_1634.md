# Q1634: Fingerprint/identity collision in NewErrInvalidCertificateProperties

## Question
Can two distinct certificates produce the same value from `NewErrInvalidCertificateProperties` (cert/errors.go) when the issuer/CA fingerprint differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/errors.go` -> `NewErrInvalidCertificateProperties` (declared at cert/errors.go:46)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the issuer/CA fingerprint; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the issuer/CA fingerprint and compare the identifier `NewErrInvalidCertificateProperties` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `NewErrInvalidCertificateProperties` returns different values for any two certificates differing in any field.
