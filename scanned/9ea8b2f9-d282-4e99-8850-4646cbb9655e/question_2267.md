# Q2267: Malleable encoding accepted by certificateV1.Copy

## Question
Does `certificateV1.Copy` (cert/cert_v1.go) accept a duplicated or out-of-order ASN.1 field in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/cert_v1.go` -> `certificateV1.Copy` (declared at cert/cert_v1.go:280)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a duplicated or out-of-order ASN.1 field; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `certificateV1.Copy` or produce an identical fingerprint.
