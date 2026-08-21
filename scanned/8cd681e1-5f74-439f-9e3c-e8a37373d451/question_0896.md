# Q0896: Nonce reuse through a duplicated counter inside the window in Bits.Update

## Question
Can an unprivileged attacker drive a duplicated counter inside the window so `Bits.Update` (bits.go) encrypts or decrypts two different messages under the same key and nonce?

## Target
- File/function: `bits.go` -> `Bits.Update` (declared at bits.go:168)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force the counter to repeat (wrap, reset, or rollback) across a rekey or reconnect and capture both ciphertexts.
- Invariant to test: For any key, no nonce value is ever used twice for encryption.
- Expected Immunefi impact: Loss of tunnel confidentiality and forgeability of traffic: catastrophic crypto failure.
- Fast validation: Invariant test recording every (key, nonce) pair produced by `Bits.Update` over a long run and asserting no repeat.
