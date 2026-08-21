# Q1721: Truncated ciphertext/tag handling in newNISTCurve

## Question
Can an attacker submit a packet whose ciphertext or tag is truncated via the boringcrypto vs stdlib path, so `newNISTCurve` (noiseutil/nist.go) slices below zero or authenticates a partial tag?

## Target
- File/function: `noiseutil/nist.go` -> `newNISTCurve` (declared at noiseutil/nist.go:22)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the boringcrypto vs stdlib path; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets one byte shorter than the minimum tag length.
- Invariant to test: Any packet shorter than header+tag minimum is rejected before decryption is attempted.
- Expected Immunefi impact: Remote panic (availability) or authentication bypass on tunnel data.
- Fast validation: Fuzz `newNISTCurve` over all lengths from 0 to the minimum, asserting clean errors.
