# Q0210: Nonce reuse through the cipher selection (AES-GCM vs ChaChaPoly) in CipherStateChaChaPoly.Overhead

## Question
Can an unprivileged attacker drive the cipher selection (AES-GCM vs ChaChaPoly) so `CipherStateChaChaPoly.Overhead` (noiseutil/chachapoly.go) encrypts or decrypts two different messages under the same key and nonce?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.Overhead` (declared at noiseutil/chachapoly.go:50)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the cipher selection (AES-GCM vs ChaChaPoly); the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force the counter to repeat (wrap, reset, or rollback) across a rekey or reconnect and capture both ciphertexts.
- Invariant to test: For any key, no nonce value is ever used twice for encryption.
- Expected Immunefi impact: Loss of tunnel confidentiality and forgeability of traffic: catastrophic crypto failure.
- Fast validation: Invariant test recording every (key, nonce) pair produced by `CipherStateChaChaPoly.Overhead` over a long run and asserting no repeat.
