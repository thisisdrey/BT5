# Q2232: Malleable encoding accepted by UnmarshalPublicKeyFromPEM

## Question
Does `UnmarshalPublicKeyFromPEM` (cert/pem.go) accept a trailing-byte ASN.1 encoding in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/pem.go` -> `UnmarshalPublicKeyFromPEM` (declared at cert/pem.go:154)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a trailing-byte ASN.1 encoding; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `UnmarshalPublicKeyFromPEM` or produce an identical fingerprint.
