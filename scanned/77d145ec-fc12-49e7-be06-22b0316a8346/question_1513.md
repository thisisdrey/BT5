# Q1513: Lighthouse answer accepted without request in hostnamesResults.GetAddrs

## Question
Does `hostnamesResults.GetAddrs` (remote_list.go) accept a relay request for a host it does not own that was never solicited, or that comes from a host that is not a configured lighthouse?

## Target
- File/function: `remote_list.go` -> `hostnamesResults.GetAddrs` (declared at remote_list.go:177)
- Entrypoint: Unauthenticated lighthouse/relay/handshake packet with attacker-chosen source address and advertised remotes
- Attacker controls: a relay request for a host it does not own; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send an unsolicited reply naming a VPN address and see whether the remote list is updated.
- Invariant to test: Only replies matching an outstanding query from a configured lighthouse are processed.
- Expected Immunefi impact: Hostmap poisoning steering a victim's traffic to an attacker-chosen underlay address.
- Fast validation: Unit test feeding an unsolicited reply into `hostnamesResults.GetAddrs` and asserting the remote list is untouched.
