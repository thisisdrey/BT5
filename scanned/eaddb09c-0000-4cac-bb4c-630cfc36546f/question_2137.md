# Q2137: Malleable encoding accepted by addASN1IntBytes

## Question
Does `addASN1IntBytes` (cert/p256/p256.go) accept a v2 certificate presented where v1 is expected in a non-canonical encoding, so two different byte strings verify as the same certificate?

## Target
- File/function: `cert/p256/p256.go` -> `addASN1IntBytes` (declared at cert/p256/p256.go:113)
- Entrypoint: Attacker-supplied certificate bytes carried in a handshake payload and passed to CA-pool verification
- Attacker controls: a v2 certificate presented where v1 is expected; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Re-encode the certificate with alternative ASN.1/protobuf framing and compare verification and fingerprint results.
- Invariant to test: Certificate parsing is strict and canonical; a certificate has exactly one accepted encoding and one fingerprint.
- Expected Immunefi impact: Blocklist/fingerprint bypass: a blocked certificate is re-encoded and accepted again.
- Fast validation: Differential test: re-encoded fixture must either fail in `addASN1IntBytes` or produce an identical fingerprint.
