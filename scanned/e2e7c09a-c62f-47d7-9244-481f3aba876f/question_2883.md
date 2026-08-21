# Q2883: Cipher selection confusion in cipherFn.Cipher

## Question
Can an attacker influence a counter far ahead of the window so `cipherFn.Cipher` (noiseutil/boring.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/boring.go` -> `cipherFn.Cipher` (declared at noiseutil/boring.go:44)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter far ahead of the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `cipherFn.Cipher` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `cipherFn.Cipher` under the other, asserting a hard failure.
