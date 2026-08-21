# Q1373: Key material lifetime in newNISTCurve

## Question
Does `newNISTCurve` (noiseutil/nist.go) retain key material past session teardown in a way reachable when a duplicated counter inside the window causes an abnormal close?

## Target
- File/function: `noiseutil/nist.go` -> `newNISTCurve` (declared at noiseutil/nist.go:22)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: a duplicated counter inside the window; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Tear down a session abnormally and inspect whether key state remains bound to a reusable index.
- Invariant to test: Key material is zeroed and unbound at teardown on every path, including error paths.
- Expected Immunefi impact: Reuse of stale keys for a new session, enabling decryption or forgery across sessions.
- Fast validation: Unit test asserting key fields are cleared after `newNISTCurve` handles both clean and error teardown.
