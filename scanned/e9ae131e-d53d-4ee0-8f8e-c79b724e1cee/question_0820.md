# Q0820: Malleable encoding accepted by unmarshalArgon2Parameters

## Question
Does `unmarshalArgon2Parameters` (cert/crypto.go) accept an oversized length prefix in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/crypto.go` -> `unmarshalArgon2Parameters` (declared at cert/crypto.go:229)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: an oversized length prefix; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `unmarshalArgon2Parameters` or produce an identical fingerprint.
