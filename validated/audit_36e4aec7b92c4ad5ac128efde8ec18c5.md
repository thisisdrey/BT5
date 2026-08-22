### Title
`parsePort` fails to validate that the beginning of a port range is ≤ the end, allowing malformed firewall rules to silently under-enforce - (File: firewall.go)

### Summary
`parsePort` in `firewall.go` parses individual firewall-rule port specs like `"10-5"` by splitting on `-` and validating each bound independently with `parsePortValue`, but it never checks that `startPort <= endPort` before returning the pair.

### Finding Description
`parsePort` validates that each half of a `"start-end"` range is a number in `[0, 65535]` via `parsePortValue`, and only special-cases the situation where `startPort == firewall.PortAny` (forcing `endPort` to `PortAny` too). It does not check the relationship between the two bounds: [1](#0-0) 

This mirrors the audited bug class in the report: `_validateDeploymentConfig` checked `maxSupply > 0` and `tokensPerMint > 0` individually but never checked the cross-field relationship `tokensPerMint <= maxSupply`, producing a config that passes validation yet can never be satisfied correctly at runtime. Here, `startPort` and `endPort` are each validated to be in range, but the invalid relational case `startPort > endPort` (e.g., a typo'd rule `"10-5"` or `"443-80"`) passes validation silently and is returned as a valid range to the rule-compilation code that builds the firewall's port-lookup table from `(startPort, endPort)`.

### Impact Explanation
A firewall rule with an inverted range is accepted by config parsing without error. Depending on how the downstream rule table iterates from `startPort` to `endPort` to populate matches, an inverted range can result in a rule that matches zero ports (an operator believes a rule is blocking/allowing a range of ports, but it silently does nothing), producing an unintended firewall bypass or unintended blocking with no error surfaced to the operator. Because firewall config can be reloaded/hot-swapped at runtime and is attacker-adjacent input in the sense that misconfigurations directly determine which traffic is allowed through the mesh, a silently-ignored rule is a security-relevant firewall enforcement gap, not merely a cosmetic issue.

### Likelihood Explanation
Likelihood is moderate: this requires an operator/config-author mistake (reversed port bounds) rather than a remote attacker directly triggering it, but such typos are plausible in hand-edited firewall configs, and there is no validation or error message today to catch it before nebula silently loads a defective rule.

### Recommendation
Add an explicit check in `parsePort` (after both bounds are parsed and before returning) that `startPort <= endPort`, returning a descriptive error such as `"ending range port is less than beginning range port"` when violated, analogous to the recommended fix of adding `require(config.tokensPerMint <= config.maxSupply, ...)` in the referenced report.

### Proof of Concept
1. Configure a firewall rule with `port: "10-5"` in a nebula config's `firewall.inbound`/`outbound` rules.
2. Call `parsePort("10-5")` — per the code at [2](#0-1) , `parsePortValue` succeeds for both `"10"` and `"5"`, and since `startPort (10) != firewall.PortAny`, the function returns `(10, 5, nil)` with no error.
3. This malformed range is passed onward to firewall rule construction; whether it results in "matches nothing" (silent bypass) or another inconsistent behavior would need to be confirmed by tracing the `AddRule`/rule-table insertion code, which I was not able to fully inspect before running out of tool calls — this is noted as an area requiring further verification in a full session. [3](#0-2)

### Citations

**File:** firewall.go (L26-28)
```go
type FirewallInterface interface {
	AddRule(incoming bool, proto uint8, startPort int32, endPort int32, groups []string, host string, cidr, localCidr string, caName string, caSha string) error
}
```

**File:** firewall.go (L1057-1096)
```go
func parsePort(s string) (int32, int32, error) {
	const notAPort int32 = -2
	if s == "any" {
		return firewall.PortAny, firewall.PortAny, nil
	}
	if s == "fragment" {
		return firewall.PortFragment, firewall.PortFragment, nil
	}
	if !strings.Contains(s, `-`) {
		rPort, err := parsePortValue("", s)
		if err != nil {
			return notAPort, notAPort, err
		}
		return rPort, rPort, nil
	}

	sPorts := strings.SplitN(s, `-`, 2)
	for i := range sPorts {
		sPorts[i] = strings.Trim(sPorts[i], " ")
	}
	if len(sPorts) != 2 || sPorts[0] == "" || sPorts[1] == "" {
		return notAPort, notAPort, fmt.Errorf("appears to be a range but could not be parsed; `%s`", s)
	}

	startPort, err := parsePortValue("beginning range ", sPorts[0])
	if err != nil {
		return notAPort, notAPort, err
	}

	endPort, err := parsePortValue("ending range ", sPorts[1])
	if err != nil {
		return notAPort, notAPort, err
	}

	if startPort == firewall.PortAny {
		endPort = firewall.PortAny
	}

	return startPort, endPort, nil
}
```
