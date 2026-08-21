# Q0640: Nonce reuse through a counter that wraps past the maximum in aeadCipher.Encrypt

## Question
Can an unprivileged attacker drive a counter that wraps past the maximum so `aeadCipher.Encrypt` (noiseutil/boring.go) encrypts or decrypts two different messages under the same key and nonce?

## Target
- File/function: `noiseutil/boring.go` -> `aeadCipher.Encrypt` (declared at noiseutil/boring.go:74)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter that wraps past the maximum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force the counter to repeat (wrap, reset, or rollback) across a rekey or reconnect and capture both ciphertexts.
- Invariant to test: For any key, no nonce value is ever used twice for encryption.
- Expected Immunefi impact: Loss of tunnel confidentiality and forgeability of traffic: catastrophic crypto failure.
- Fast validation: Invariant test recording every (key, nonce) pair produced by `aeadCipher.Encrypt` over a long run and asserting no repeat.
