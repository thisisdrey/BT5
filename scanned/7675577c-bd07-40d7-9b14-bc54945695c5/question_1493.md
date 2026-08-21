# Q1493: Truncated ciphertext/tag handling in CipherStateChaChaPoly.Overhead

## Question
Can an attacker submit a packet whose ciphertext or tag is truncated via the replay bit window, so `CipherStateChaChaPoly.Overhead` (noiseutil/chachapoly.go) slices below zero or authenticates a partial tag?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.Overhead` (declared at noiseutil/chachapoly.go:50)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the replay bit window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets one byte shorter than the minimum tag length.
- Invariant to test: Any packet shorter than header+tag minimum is rejected before decryption is attempted.
- Expected Immunefi impact: Remote panic (availability) or authentication bypass on tunnel data.
- Fast validation: Fuzz `CipherStateChaChaPoly.Overhead` over all lengths from 0 to the minimum, asserting clean errors.
