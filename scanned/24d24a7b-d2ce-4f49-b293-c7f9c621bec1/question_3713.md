# Q3713: Cipher selection confusion in Bits.clearRange

## Question
Can an attacker influence the key rotation/rekey boundary so `Bits.clearRange` (bits.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `bits.go` -> `Bits.clearRange` (declared at bits.go:66)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `Bits.clearRange` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `Bits.clearRange` under the other, asserting a hard failure.
