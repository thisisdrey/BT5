# Q0742: Cipher selection confusion in NewCipherStateChaChaPoly

## Question
Can an attacker influence a counter that wraps past the maximum so `NewCipherStateChaChaPoly` (noiseutil/chachapoly.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/chachapoly.go` -> `NewCipherStateChaChaPoly` (declared at noiseutil/chachapoly.go:19)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter that wraps past the maximum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `NewCipherStateChaChaPoly` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `NewCipherStateChaChaPoly` under the other, asserting a hard failure.
