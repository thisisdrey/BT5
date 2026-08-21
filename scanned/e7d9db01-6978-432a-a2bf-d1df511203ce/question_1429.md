# Q1429: Relay authorization in newCalculatedRemotesEntryFromConfig

## Question
Does `newCalculatedRemotesEntryFromConfig` (calculated_remote.go) verify that the requester is authorized to relay for the named target when handling a spoofed UDP source address?

## Target
- File/function: `calculated_remote.go` -> `newCalculatedRemotesEntryFromConfig` (declared at calculated_remote.go:126)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a spoofed UDP source address; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Request a relay for a pair the requester has no relationship with and observe whether state is created.
- Invariant to test: Relay state is created only for peers explicitly permitted by configuration and proven by authenticated traffic.
- Expected Immunefi impact: Unauthorized use of a node as a relay, enabling traffic interposition or resource abuse.
- Fast validation: Unit test requesting an unauthorized relay through `newCalculatedRemotesEntryFromConfig` and asserting refusal.
