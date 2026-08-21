# Q0216: Nonce reuse through the cipher selection (AES-GCM vs ChaChaPoly) in nistCurve.DHLen

## Question
Can an unprivileged attacker drive the cipher selection (AES-GCM vs ChaChaPoly) so `nistCurve.DHLen` (noiseutil/nist.go) encrypts or decrypts two different messages under the same key and nonce?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DHLen` (declared at noiseutil/nist.go:57)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the cipher selection (AES-GCM vs ChaChaPoly); the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force the counter to repeat (wrap, reset, or rollback) across a rekey or reconnect and capture both ciphertexts.
- Invariant to test: For any key, no nonce value is ever used twice for encryption.
- Expected Immunefi impact: Loss of tunnel confidentiality and forgeability of traffic: catastrophic crypto failure.
- Fast validation: Invariant test recording every (key, nonce) pair produced by `nistCurve.DHLen` over a long run and asserting no repeat.
