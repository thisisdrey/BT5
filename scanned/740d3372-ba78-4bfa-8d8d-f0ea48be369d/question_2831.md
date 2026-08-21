# Q2831: Fingerprint/identity collision in DecryptAndUnmarshalSigningPrivateKey

## Question
Can two distinct certificates produce the same value from `DecryptAndUnmarshalSigningPrivateKey` (cert/crypto.go) when the UnsafeNetworks field differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `DecryptAndUnmarshalSigningPrivateKey` (declared at cert/crypto.go:255)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the UnsafeNetworks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the UnsafeNetworks field and compare the identifier `DecryptAndUnmarshalSigningPrivateKey` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `DecryptAndUnmarshalSigningPrivateKey` returns different values for any two certificates differing in any field.
