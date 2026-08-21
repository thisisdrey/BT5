# Q1142: Route lookup manipulation in flipBytes

## Question
Can an attacker influence a route with an attacker-influenced metric so `flipBytes` (overlay/tun.go) selects a route or gateway other than the configured one?

## Target
- File/function: `overlay/tun.go` -> `flipBytes` (declared at overlay/tun.go:95)
- Entrypoint: Attacker-authored inner packet reaching the tun write path, or attacker-influenced route/MTU state
- Attacker controls: a route with an attacker-influenced metric; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send traffic crafted to shift the route selection inputs and observe the chosen next hop.
- Invariant to test: Route selection depends only on local configuration and the packet's destination, never on peer-supplied data.
- Expected Immunefi impact: Traffic redirection out an unintended interface or gateway.
- Fast validation: Unit test asserting `flipBytes` returns the configured route for every attacker-shaped input.
