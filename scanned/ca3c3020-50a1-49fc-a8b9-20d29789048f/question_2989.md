# Q2989: Route lookup manipulation in procyield

## Question
Can an attacker influence an MTU-sized packet forcing a split write so `procyield` (wintun/tun.go) selects a route or gateway other than the configured one?

## Target
- File/function: `wintun/tun.go` -> `procyield` (declared at wintun/tun.go:57)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: an MTU-sized packet forcing a split write; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `procyield` returns the configured route for every attacker-shaped input.
