# Q1082: Fingerprint/identity collision in TBSCertificate.SignWith

## Question
Can two distinct certificates produce the same value from `TBSCertificate.SignWith` (cert/sign.go) when a trailing-byte ASN.1 encoding differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/sign.go` -> `TBSCertificate.SignWith` (declared at cert/sign.go:77)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a trailing-byte ASN.1 encoding and compare the identifier `TBSCertificate.SignWith` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `TBSCertificate.SignWith` returns different values for any two certificates differing in any field.
