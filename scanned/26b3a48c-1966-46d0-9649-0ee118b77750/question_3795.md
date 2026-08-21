# Q3795: Cipher selection confusion in Bits.strictlyWithinWindow

## Question
Can an attacker influence the boringcrypto vs stdlib path so `Bits.strictlyWithinWindow` (bits.go) decrypts with a different cipher or key schedule than the peer used to encrypt?

## Target
- File/function: `bits.go` -> `Bits.strictlyWithinWindow` (declared at bits.go:120)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the boringcrypto vs stdlib path; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise or flip the cipher indicator and observe which routine `Bits.strictlyWithinWindow` dispatches to.
- Invariant to test: Cipher choice is fixed by the verified handshake and never re-derived from unauthenticated per-packet data.
- Expected Immunefi impact: Crypto misuse enabling forged or malleable tunnel traffic.
- Fast validation: Differential test encrypting with one cipher and decrypting through `Bits.strictlyWithinWindow` under the other, asserting a hard failure.
