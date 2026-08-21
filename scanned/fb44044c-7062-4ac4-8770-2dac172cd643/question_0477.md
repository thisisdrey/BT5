# Q0477: Counter rollback/reset on rekey in CipherStateAESGCM.Overhead

## Question
Does `CipherStateAESGCM.Overhead` (noiseutil/aesgcm.go) reset a counter far ahead of the window on rekey or reconnect without also guaranteeing a fresh key, allowing counters to be reused under an old key?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.Overhead` (declared at noiseutil/aesgcm.go:51)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a counter far ahead of the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Force a rekey path where the key is retained but the counter resets.
- Invariant to test: Counter resets happen only simultaneously with installation of fresh key material.
- Expected Immunefi impact: Nonce reuse leading to plaintext recovery and packet forgery.
- Fast validation: Unit test asserting `CipherStateAESGCM.Overhead` never lowers the counter without a key change.
