# Q2083: Expiry window handling in EncryptAndMarshalSigningPrivateKey

## Question
Can a duplicated or out-of-order ASN.1 field make `EncryptAndMarshalSigningPrivateKey` (cert/crypto.go) treat a certificate as valid outside its NotBefore/NotAfter window, for example through inverted, zero, or overflowing timestamps?

## Target
- File/function: `cert/crypto.go` -> `EncryptAndMarshalSigningPrivateKey` (declared at cert/crypto.go:160)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Set NotAfter before NotBefore, or use extreme timestamps, and check the comparison in `EncryptAndMarshalSigningPrivateKey`.
- Invariant to test: A certificate is valid only when NotBefore <= now <= NotAfter, with degenerate windows rejected outright.
- Expected Immunefi impact: Use of expired or not-yet-valid credentials to obtain overlay access.
- Fast validation: Table-driven unit test over degenerate time windows asserting `EncryptAndMarshalSigningPrivateKey` rejects each.
