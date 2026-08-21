# Q2969: Cipher selection confusion in cipherFn.CipherName

## Question
Can an attacker influence a duplicated counter inside the window so `cipherFn.CipherName` (noiseutil/boring.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/boring.go` -> `cipherFn.CipherName` (declared at noiseutil/boring.go:45)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `cipherFn.CipherName` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `cipherFn.CipherName` under the other, asserting a hard failure.
