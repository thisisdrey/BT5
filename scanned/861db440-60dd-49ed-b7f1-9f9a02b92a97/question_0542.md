# Q0542: Fingerprint/identity collision in readOptionalASN1Byte

## Question
Can two distinct certificates produce the same value from `readOptionalASN1Byte` (cert/asn1.go) when a trailing-byte ASN.1 encoding differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/asn1.go` -> `readOptionalASN1Byte` (declared at cert/asn1.go:33)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a trailing-byte ASN.1 encoding and compare the identifier `readOptionalASN1Byte` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `readOptionalASN1Byte` returns different values for any two certificates differing in any field.
