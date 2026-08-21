# Q1006: Cipher selection confusion in nistCurve.GenerateKeypair

## Question
Can an attacker influence a counter far ahead of the window so `nistCurve.GenerateKeypair` (noiseutil/nist.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.GenerateKeypair` (declared at noiseutil/nist.go:32)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter far ahead of the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `nistCurve.GenerateKeypair` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `nistCurve.GenerateKeypair` under the other, asserting a hard failure.
