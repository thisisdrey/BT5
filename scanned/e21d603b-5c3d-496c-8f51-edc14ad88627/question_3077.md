# Q3077: Fragmented/short inner packet handling in LocalAllowList.AllowName

## Question
Can an attacker send the sending certificate's groups so `LocalAllowList.AllowName` (allow_list.go) cannot read the ports or protocol and defaults to allow rather than deny?

## Target
- File/function: `allow_list.go` -> `LocalAllowList.AllowName` (declared at allow_list.go:255)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the sending certificate's groups; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Send a fragment with a non-zero offset or a header too short to contain ports.
- Invariant to test: Packets whose policy-relevant fields cannot be parsed are dropped, never allowed by default.
- Expected Immunefi impact: Firewall bypass delivering attacker payloads to inside services on denied ports.
- Fast validation: Table-driven unit test over unparsable inner packets asserting `LocalAllowList.AllowName` returns deny.
