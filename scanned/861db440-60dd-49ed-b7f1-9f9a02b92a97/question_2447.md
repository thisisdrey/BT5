# Q2447: Inner-header trust in NewLocalAllowListFromConfig

## Question
Does `NewLocalAllowListFromConfig` (allow_list.go) trust the inner IP header's the sending certificate's CA name/SHA rather than the session's authenticated identity when deciding direction or source?

## Target
- File/function: `allow_list.go` -> `NewLocalAllowListFromConfig` (declared at allow_list.go:37)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's CA name/SHA; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a tunnel packet whose inner source address is another host's VPN address.
- Invariant to test: The inner source address must be contained in the sending certificate's Networks or the packet is dropped.
- Expected Immunefi impact: Source spoofing on the overlay, bypassing rules keyed on the source host.
- Fast validation: Unit test sending a packet with a foreign inner source through `NewLocalAllowListFromConfig`, asserting a drop.
