# Q2741: Fingerprint/identity collision in EncryptAndMarshalSigningPrivateKey

## Question
Can two distinct certificates produce the same value from `EncryptAndMarshalSigningPrivateKey` (cert/crypto.go) when a self-signed certificate differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `EncryptAndMarshalSigningPrivateKey` (declared at cert/crypto.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a self-signed certificate; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in a self-signed certificate and compare the identifier `EncryptAndMarshalSigningPrivateKey` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `EncryptAndMarshalSigningPrivateKey` returns different values for any two certificates differing in any field.
