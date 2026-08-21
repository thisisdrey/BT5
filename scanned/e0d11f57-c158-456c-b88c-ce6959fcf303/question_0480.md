# Q0480: Counter rollback/reset on rekey in CipherStateChaChaPoly.Overhead

## Question
Does `CipherStateChaChaPoly.Overhead` (noiseutil/chachapoly.go) reset a counter far ahead of the window on rekey or reconnect without also guaranteeing a fresh key, allowing counters to be reused under an old key?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.Overhead` (declared at noiseutil/chachapoly.go:50)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter far ahead of the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force a rekey path where the key is retained but the counter resets.
- Invariant to test: Counter resets happen only simultaneously with installation of fresh key material.
- Expected Immunefi impact: Nonce reuse leading to plaintext recovery and packet forgery.
- Fast validation: Unit test asserting `CipherStateChaChaPoly.Overhead` never lowers the counter without a key change.
