# Q1027: Nonce reuse through the key rotation/rekey boundary in Bits.updateSlow

## Question
Can an unprivileged attacker drive the key rotation/rekey boundary so `Bits.updateSlow` (bits.go) encrypts or decrypts two different messages under the same key and nonce?

## Target
- File/function: `bits.go` -> `Bits.updateSlow` (declared at bits.go:189)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force the counter to repeat (wrap, reset, or rollback) across a rekey or reconnect and capture both ciphertexts.
- Invariant to test: For any key, no nonce value is ever used twice for encryption.
- Expected Immunefi impact: Loss of tunnel confidentiality and forgeability of traffic: catastrophic crypto failure.
- Fast validation: Invariant test recording every (key, nonce) pair produced by `Bits.updateSlow` over a long run and asserting no repeat.
