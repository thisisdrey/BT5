# Q3218: Third-party address advertisement in RemoteList.unlockedPrependV6

## Question
Can an attacker advertise a punch notification naming an arbitrary target through `RemoteList.unlockedPrependV6` (remote_list.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `remote_list.go` -> `RemoteList.unlockedPrependV6` (declared at remote_list.go:521)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a punch notification naming an arbitrary target; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `RemoteList.unlockedPrependV6` never emits packets to an address advertised for a foreign identity.
