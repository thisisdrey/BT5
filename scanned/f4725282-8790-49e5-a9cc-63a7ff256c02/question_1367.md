# Q1367: Boring/stdlib path divergence in CipherStateAESGCM.DecryptDanger

## Question
Do the boringcrypto and stdlib builds of `CipherStateAESGCM.DecryptDanger` (noiseutil/aesgcm.go) differ in how they handle the message counter, so one accepts what the other rejects?

## Target
- File/function: `noiseutil/aesgcm.go` -> `CipherStateAESGCM.DecryptDanger` (declared at noiseutil/aesgcm.go:39)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the message counter; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Run identical malformed inputs through both build tags and compare acceptance.
- Invariant to test: Both crypto backends enforce identical acceptance rules for every input.
- Expected Immunefi impact: A build-dependent crypto acceptance gap admitting forged or replayed traffic on one platform.
- Fast validation: Differential test running the same vectors under both build tags and asserting identical results.
