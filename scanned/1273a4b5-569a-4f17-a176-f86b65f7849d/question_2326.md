# Q2326: Malleable encoding accepted by UnmarshalSigningPublicKeyFromPEM

## Question
Does `UnmarshalSigningPublicKeyFromPEM` (cert/pem.go) accept a v1 certificate presented where v2 is expected in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/pem.go` -> `UnmarshalSigningPublicKeyFromPEM` (declared at cert/pem.go:181)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v1 certificate presented where v2 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `UnmarshalSigningPublicKeyFromPEM` or produce an identical fingerprint.
