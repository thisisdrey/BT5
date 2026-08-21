# Q1396: Malleable encoding accepted by IsNormalized

## Question
Does `IsNormalized` (cert/p256/p256.go) accept the UnsafeNetworks field in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/p256/p256.go` -> `IsNormalized` (declared at cert/p256/p256.go:25)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the UnsafeNetworks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `IsNormalized` or produce an identical fingerprint.
