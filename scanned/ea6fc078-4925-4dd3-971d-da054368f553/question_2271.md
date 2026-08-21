# Q2271: Malleable encoding accepted by detailsV2.Marshal

## Question
Does `detailsV2.Marshal` (cert/cert_v2.go) accept the Networks field in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/cert_v2.go` -> `detailsV2.Marshal` (declared at cert/cert_v2.go:484)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: the Networks field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `detailsV2.Marshal` or produce an identical fingerprint.
