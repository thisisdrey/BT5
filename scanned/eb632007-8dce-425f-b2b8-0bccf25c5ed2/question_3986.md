# Q3986: Local CIDR restriction bypass in newAllowListFromConfig

## Question
Can the inner protocol number bypass the local_cidr restriction enforced in `newAllowListFromConfig` (allow_list.go), letting traffic reach a local destination the rule excludes?

## Target
- File/function: `allow_list.go` -> `newAllowListFromConfig` (declared at allow_list.go:73)
- Entrypoint: Tunnel packet whose inner IP header and payload are fully attacker-authored
- Attacker controls: the inner protocol number; the attacker holds no CA-signed certificate, no host or root access, no leaked keys, and no configuration control.
- Exploit idea: Address traffic just outside/inside the local_cidr boundary and observe the decision.
- Invariant to test: local_cidr containment is checked on the actual write destination for every allowed packet.
- Expected Immunefi impact: Firewall bypass reaching restricted local destinations, including the host itself.
- Fast validation: Table-driven unit test over local_cidr boundaries asserting `newAllowListFromConfig` denies out-of-range destinations.
