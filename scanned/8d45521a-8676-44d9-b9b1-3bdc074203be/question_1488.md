# Q1488: Truncated ciphertext/tag handling in CipherStateAESGCM.EncryptDanger

## Question
Can an attacker submit a packet whose ciphertext or tag is truncated via the message counter, so `CipherStateAESGCM.EncryptDanger` (noiseutil/aesgcm.go) slices below zero or authenticates a partial tag?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.EncryptDanger` (declared at noiseutil/aesgcm.go:24)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send packets one byte shorter than the minimum tag length.
- Invariant to test: Any packet shorter than header+tag minimum is rejected before decryption is attempted.
- Expected Immunefi impact: Remote panic (availability) or authentication bypass on tunnel data.
- Fast validation: Fuzz `CipherStateAESGCM.EncryptDanger` over all lengths from 0 to the minimum, asserting clean errors.
