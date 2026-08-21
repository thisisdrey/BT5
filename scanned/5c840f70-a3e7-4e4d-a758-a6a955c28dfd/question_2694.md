# Q2694: Third-party address advertisement in RemoteList.ResetForOwner

## Question
Can an attacker advertise an oversized remote list through `RemoteList.ResetForOwner` (remote_list.go) to make a victim node send traffic to an unrelated third party?

## Target
- File/function: `remote_list.go` -> `RemoteList.ResetForOwner` (declared at remote_list.go:244)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: an oversized remote list; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Advertise a remote pointing at a chosen victim IP:port and observe the node's outbound traffic.
- Invariant to test: Advertised remotes are only ever used for the identity that owns them, and are filtered against configured ranges.
- Expected Immunefi impact: Traffic amplification/reflection using nodes as unwitting senders toward a third-party target.
- Fast validation: Integration test asserting `RemoteList.ResetForOwner` never emits packets to an address advertised for a foreign identity.
