# Q1722: Truncated ciphertext/tag handling in nistCurve.GenerateKeypair

## Question
Can an attacker submit a packet whose ciphertext or tag is truncated via the message counter, so `nistCurve.GenerateKeypair` (noiseutil/nist.go) slices below zero or authenticates a partial tag?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.GenerateKeypair` (declared at noiseutil/nist.go:32)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets one byte shorter than the minimum tag length.
- Invariant to test: Any packet shorter than header+tag minimum is rejected before decryption is attempted.
- Expected Immunefi impact: Remote panic (availability) or authentication bypass on tunnel data.
- Fast validation: Fuzz `nistCurve.GenerateKeypair` over all lengths from 0 to the minimum, asserting clean errors.
