# Q0710: Rekey/roaming state confusion in Credential.buildHandshakeState

## Question
During rekey or roam handling, can an attacker use a handshake reusing a prior ephemeral key so `Credential.buildHandshakeState` (handshake/credential.go) swaps in keys or a remote address that the peer never authorized?

## Target
- File/function: `handshake/credential.go` -> `Credential.buildHandshakeState` (declared at handshake/credential.go:38)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake reusing a prior ephemeral key; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Interleave a crafted handshake with an active session's rekey and observe which key/remote wins in `Credential.buildHandshakeState`.
- Invariant to test: Key and remote updates apply only after the new material is cryptographically verified as coming from the same peer identity.
- Expected Immunefi impact: Traffic hijack or MITM redirection of an established tunnel.
- Fast validation: Integration test racing a forged rekey against a genuine one, asserting the forged material is discarded.
