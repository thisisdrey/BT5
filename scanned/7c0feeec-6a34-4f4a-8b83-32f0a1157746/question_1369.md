# Q1369: Truncated ciphertext/tag handling in NewCipherStateAESGCM

## Question
Can an attacker submit a packet whose ciphertext or tag is truncated via the boringcrypto vs stdlib path, so `NewCipherStateAESGCM` (noiseutil/aesgcm.go) slices below zero or authenticates a partial tag?

## Target
- File/function: `noiseutil/aesgcm.go` -> `NewCipherStateAESGCM` (declared at noiseutil/aesgcm.go:20)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the boringcrypto vs stdlib path; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets one byte shorter than the minimum tag length.
- Invariant to test: Any packet shorter than header+tag minimum is rejected before decryption is attempted.
- Expected Immunefi impact: Remote panic (availability) or authentication bypass on tunnel data.
- Fast validation: Fuzz `NewCipherStateAESGCM` over all lengths from 0 to the minimum, asserting clean errors.
