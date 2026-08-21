# Q3842: Rekey/roaming state confusion in NewMachine

## Question
During rekey or roam handling, can an attacker use a handshake arriving after the timer wheel expired the entry so `NewMachine` (handshake/machine.go) swaps in keys or a remote address that the peer never authorized?

## Target
- File/function: `handshake/machine.go` -> `NewMachine` (declared at handshake/machine.go:76)
- Entrypoint: Unauthenticated handshake packet (`header.Handshake`) from a host holding no CA-signed certificate
- Attacker controls: a handshake arriving after the timer wheel expired the entry; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Interleave a crafted handshake with an active session's rekey and observe which key/remote wins in `NewMachine`.
- Invariant to test: Key and remote updates apply only after the new material is cryptographically verified as coming from the same peer identity.
- Expected Immunefi impact: Traffic hijack or MITM redirection of an established tunnel.
- Fast validation: Integration test racing a forged rekey against a genuine one, asserting the forged material is discarded.
