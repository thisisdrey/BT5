# Q2140: Counter rollback/reset on rekey in aeadCipher.Decrypt

## Question
Does `aeadCipher.Decrypt` (noiseutil/boring.go) reset the key rotation/rekey boundary on rekey or reconnect without also guaranteeing a fresh key, allowing counters to be reused under an old key?

## Target
- File/function: `noiseutil/boring.go` -> `aeadCipher.Decrypt` (declared at noiseutil/boring.go:78)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force a rekey path where the key is retained but the counter resets.
- Invariant to test: Counter resets happen only simultaneously with installation of fresh key material.
- Expected Immunefi impact: Nonce reuse leading to plaintext recovery and packet forgery.
- Fast validation: Unit test asserting `aeadCipher.Decrypt` never lowers the counter without a key change.
