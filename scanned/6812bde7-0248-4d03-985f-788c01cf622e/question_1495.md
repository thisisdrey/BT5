# Q1495: Key material lifetime in nistCurve.DHName

## Question
Does `nistCurve.DHName` (noiseutil/nist.go) retain key material past session teardown in a way reachable when the nonce construction causes an abnormal close?

## Target
- File/function: `noiseutil/nist.go` -> `nistCurve.DHName` (declared at noiseutil/nist.go:68)
- Entrypoint: Attacker-chosen ciphertext, message counter, and header bytes on the wire for an existing or forming session
- Attacker controls: the nonce construction; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Tear down a session abnormally and inspect whether key state remains bound to a reusable index.
- Invariant to test: Key material is zeroed and unbound at teardown on every path, including error paths.
- Expected Immunefi impact: Reuse of stale keys for a new session, enabling decryption or forgery across sessions.
- Fast validation: Unit test asserting key fields are cleared after `nistCurve.DHName` handles both clean and error teardown.
