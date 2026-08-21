# Q3631: Cipher selection confusion in Bits.set

## Question
Can an attacker influence a duplicated counter inside the window so `Bits.set` (bits.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `bits.go` -> `Bits.set` (declared at bits.go:58)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `Bits.set` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `Bits.set` under the other, asserting a hard failure.
