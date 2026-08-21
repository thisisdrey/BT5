# Q1496: Boring/stdlib path divergence in newNISTCurve

## Question
Do the boringcrypto and stdlib builds of `newNISTCurve` (noiseutil/nist.go) differ in how they handle the key rotation/rekey boundary, so one accepts what the other rejects?

## Target
- File/function: `noiseutil/nist.go` -> `newNISTCurve` (declared at noiseutil/nist.go:22)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the key rotation/rekey boundary; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Run identical malformed inputs through both build tags and compare acceptance.
- Invariant to test: Both crypto backends enforce identical acceptance rules for every input.
- Expected Immunefi impact: A build-dependent crypto acceptance gap admitting forged or replayed traffic on one platform.
- Fast validation: Differential test running the same vectors under both build tags and asserting identical results.
