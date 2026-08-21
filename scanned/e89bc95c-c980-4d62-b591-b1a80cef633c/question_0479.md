# Q0479: Counter rollback/reset on rekey in CipherStateChaChaPoly.DecryptDanger

## Question
Does `CipherStateChaChaPoly.DecryptDanger` (noiseutil/chachapoly.go) reset a counter that wraps past the maximum on rekey or reconnect without also guaranteeing a fresh key, allowing counters to be reused under an old key?

## Target
- File/function: `noiseutil/chachapoly.go` -> `CipherStateChaChaPoly.DecryptDanger` (declared at noiseutil/chachapoly.go:38)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter that wraps past the maximum; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force a rekey path where the key is retained but the counter resets.
- Invariant to test: Counter resets happen only simultaneously with installation of fresh key material.
- Expected Immunefi impact: Nonce reuse leading to plaintext recovery and packet forgery.
- Fast validation: Unit test asserting `CipherStateChaChaPoly.DecryptDanger` never lowers the counter without a key change.
