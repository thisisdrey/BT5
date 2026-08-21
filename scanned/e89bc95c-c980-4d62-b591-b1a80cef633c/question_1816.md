# Q1816: Route lookup manipulation in Interface.onFatal

## Question
Can an attacker influence a route with an attacker-influenced metric so `Interface.onFatal` (interface.go) selects a route or gateway other than the configured one?

## Target
- File/function: `interface.go` -> `Interface.onFatal` (declared at interface.go:299)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `Interface.onFatal` returns the configured route for every attacker-shaped input.
