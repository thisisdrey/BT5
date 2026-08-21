# Q3224: Cipher selection confusion in aeadCipher.Decrypt

## Question
Can an attacker influence the message counter so `aeadCipher.Decrypt` (noiseutil/boring.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/boring.go` -> `aeadCipher.Decrypt` (declared at noiseutil/boring.go:78)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `aeadCipher.Decrypt` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `aeadCipher.Decrypt` under the other, asserting a hard failure.
