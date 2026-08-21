# Q3054: Cipher selection confusion in cipherAESGCMBoring

## Question
Can an attacker influence the key rotation/rekey boundary so `cipherAESGCMBoring` (noiseutil/boring.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `noiseutil/boring.go` -> `cipherAESGCMBoring` (declared at noiseutil/boring.go:50)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `cipherAESGCMBoring` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `cipherAESGCMBoring` under the other, asserting a hard failure.
