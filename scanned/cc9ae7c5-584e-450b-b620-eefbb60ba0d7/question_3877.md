# Q3877: Cipher selection confusion in Bits.Check

## Question
Can an attacker influence the message counter so `Bits.Check` (bits.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `bits.go` -> `Bits.Check` (declared at bits.go:135)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `Bits.Check` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `Bits.Check` under the other, asserting a hard failure.
