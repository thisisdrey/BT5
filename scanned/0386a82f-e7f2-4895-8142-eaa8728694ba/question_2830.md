# Q2830: Fingerprint/identity collision in unmarshalArgon2Parameters

## Question
Can two distinct certificates produce the same value from `unmarshalArgon2Parameters` (cert/crypto.go) when the Networks field differs, causing blocklist or hostmap keying to collide?

## Target
- File/function: `cert/crypto.go` -> `unmarshalArgon2Parameters` (declared at cert/crypto.go:229)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Construct two certificates differing only in the Networks field and compare the identifier `unmarshalArgon2Parameters` produces.
- Invariant to test: The certificate identifier is a collision-resistant function of the complete signed bytes.
- Expected Immunefi impact: Blocklist evasion or identity impersonation on the overlay.
- Fast validation: Unit test asserting `unmarshalArgon2Parameters` returns different values for any two certificates differing in any field.
