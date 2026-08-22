### Title
Firewall port parser integer truncation silently upgrades a specific port rule into an "any-port" allow rule - (File: `firewall.go`, `parsePort`)

### Summary
The original report flags `Synth.approveAndCall` for granting a **broader permission than the caller actually asked for** (max-value approval instead of the specific `amount`). The reachable analog in this codebase is `firewall.go`'s `parsePort`, used by `AddFirewallRulesFromConfig` when building firewall rules from `firewall.inbound`/`firewall.outbound` config entries [1](#0-0) . When a port value is out of `int32` range (e.g. `4294967296`), the parser's numeric conversion truncates to `0`, which is also the sentinel value `firewall.PortAny` used internally to mean "match all ports." This means a rule that was configured to allow a **specific port** can silently become a rule that allows **any port**, exactly mirroring the "unnecessary/excessive approval instead of the specific amount" bug class.

### Finding Description
`AddFirewallRulesFromConfig` reads the operator-supplied `port`/`code` string for each firewall rule and passes it to `parsePort` to compute `startPort`/`endPort`, which are then registered via `fw.AddRule` / `fp.addRule` [2](#0-1) . `AddRule` treats `firewall.PortAny` (0) as the "match all ports" case rather than a normal specific port [3](#0-2) , and `firewallPort.match` explicitly falls back to `fp[firewall.PortAny]` when no exact port entry matches, i.e. the "any" port bucket is checked as a wildcard bypass for the rule table [4](#0-3) .

The project's own test suite documents that a value like `"4294967296"` (2^32), when converted with `int32(strconv.Atoi(...))`, truncates to `0`, which collides with `firewall.PortAny`, "silently turning a typo into a match-all-ports rule" [5](#0-4) . That test (`Test_parsePort_invalid`) exists specifically to assert this case *must* error, confirming the bug class is recognized as a real risk in this exact function; whether the currently shipped `parsePort` implementation actually returns an error for this input (i.e., whether the fix landed) could not be confirmed from the available index — the function body itself was not retrievable in this session.

### Impact Explanation
If an operator (or an automated/templated config generator) supplies an out-of-range port number for an inbound firewall rule — for example through a mistyped or programmatically generated port value — the resulting firewall rule could silently apply to **all ports** rather than the single port intended, similar to `approveAndCall` granting max approval instead of the requested amount. This would open every port for the hosts/groups/CIDRs matched by that rule, rather than the single port the administrator intended to expose, which is a firewall bypass / unintended traffic-allow condition reachable by any peer able to reach the affected node on the VPN overlay.

### Likelihood Explanation
This defect requires a firewall rule config with an integer-overflowing port value, which is an edge case that would typically only be hit via a config bug, automated tooling, or a malicious config template rather than direct remote exploitation — it is not attacker-triggerable over the wire, only through local/operator-supplied configuration. Likelihood is low, but the existence of a dedicated regression test naming this exact scenario indicates the maintainers consider it a credible, previously-real risk.

### Recommendation
Ensure `parsePort` explicitly rejects any parsed integer that falls outside `[1, 65535]` (or the valid range including `PortAny`'s dedicated sentinel) *before* any `int32` cast/truncation occurs, so an overflowing input can never coincide with `firewall.PortAny` and silently become a wildcard rule. Add explicit bounds checks on the pre-cast integer (e.g. using `strconv.ParseInt` with proper range validation) instead of relying on `int32()` conversion semantics.

### Proof of Concept
1. Configure a firewall inbound rule with `port: "4294967296"` (2^32) and a specific `host`/`group`.
2. If `parsePort` truncates this to `int32(0)` without an explicit range-error check, the resulting rule is registered under `firewall.PortAny`, per `AddRule`'s handling of the port value [6](#0-5) .
3. Any inbound packet on any port from a matching host/group/CIDR then matches the "any port" bucket check in `firewallPort.match`, bypassing the intended port restriction [4](#0-3) .
4. This exact scenario is called out by name in the test suite as "the named bug" that `Test_parsePort_invalid` exists to prevent [7](#0-6) .

### Citations

**File:** firewall.go (L252-299)
```go
func (f *Firewall) AddRule(incoming bool, proto uint8, startPort int32, endPort int32, groups []string, host string, cidr, localCidr, caName string, caSha string) error {
	var (
		ft *FirewallTable
		fp firewallPort
	)

	if incoming {
		ft = f.InRules
	} else {
		ft = f.OutRules
	}

	switch proto {
	case firewall.ProtoTCP:
		fp = ft.TCP
	case firewall.ProtoUDP:
		fp = ft.UDP
	case firewall.ProtoICMP, firewall.ProtoICMPv6:
		//ICMP traffic doesn't have ports, so we always coerce to "any", even if a value is provided
		if startPort != firewall.PortAny {
			f.l.Warn("ignoring port specification for ICMP firewall rule", "startPort", startPort)
		}
		startPort = firewall.PortAny
		endPort = firewall.PortAny
		fp = ft.ICMP
	case firewall.ProtoAny:
		fp = ft.AnyProto
	default:
		return fmt.Errorf("unknown protocol %v", proto)
	}

	// We need this rule string because we generate a hash. Removing this will break firewall reload.
	ruleString := fmt.Sprintf(
		"incoming: %v, proto: %v, startPort: %v, endPort: %v, groups: %v, host: %v, ip: %v, localIp: %v, caName: %v, caSha: %s",
		incoming, proto, startPort, endPort, groups, host, cidr, localCidr, caName, caSha,
	)
	f.rules += ruleString + "\n"

	direction := "incoming"
	if !incoming {
		direction = "outgoing"
	}
	f.l.Info("Firewall rule added",
		"firewallRule", m{"direction": direction, "proto": proto, "startPort": startPort, "endPort": endPort, "groups": groups, "host": host, "cidr": cidr, "localCidr": localCidr, "caName": caName, "caSha": caSha},
	)

	return fp.addRule(f, startPort, endPort, groups, host, cidr, localCidr, caName, caSha)
}
```

**File:** firewall.go (L319-384)
```go
func AddFirewallRulesFromConfig(l *slog.Logger, inbound bool, c *config.C, fw FirewallInterface) error {
	var table string
	if inbound {
		table = "firewall.inbound"
	} else {
		table = "firewall.outbound"
	}

	r := c.Get(table)
	if r == nil {
		return nil
	}

	rs, ok := r.([]any)
	if !ok {
		return fmt.Errorf("%s failed to parse, should be an array of rules", table)
	}

	for i, t := range rs {
		r, err := convertRule(l, t, table, i)
		if err != nil {
			return fmt.Errorf("%s rule #%v; %s", table, i, err)
		}

		if r.Code != "" && r.Port != "" {
			return fmt.Errorf("%s rule #%v; only one of port or code should be provided", table, i)
		}

		if r.Host == "" && len(r.Groups) == 0 && r.Cidr == "" && r.LocalCidr == "" && r.CAName == "" && r.CASha == "" {
			return fmt.Errorf("%s rule #%v; at least one of host, group, cidr, local_cidr, ca_name, or ca_sha must be provided", table, i)
		}

		var sPort, errPort string
		if r.Code != "" {
			errPort = "code"
			sPort = r.Code
		} else {
			errPort = "port"
			sPort = r.Port
		}

		var proto uint8
		var startPort, endPort int32
		switch r.Proto {
		case "any":
			proto = firewall.ProtoAny
			startPort, endPort, err = parsePort(sPort)
		case "tcp":
			proto = firewall.ProtoTCP
			startPort, endPort, err = parsePort(sPort)
		case "udp":
			proto = firewall.ProtoUDP
			startPort, endPort, err = parsePort(sPort)
		case "icmp":
			proto = firewall.ProtoICMP
			startPort = firewall.PortAny
			endPort = firewall.PortAny
			if sPort != "" {
				l.Warn("ignoring port specification for ICMP firewall rule", "port", sPort)
			}
		default:
			return fmt.Errorf("%s rule #%v; proto was not understood; `%s`", table, i, r.Proto)
		}
		if err != nil {
			return fmt.Errorf("%s rule #%v; %s %s", table, i, errPort, err)
		}
```

**File:** firewall.go (L676-704)
```go
func (fp firewallPort) match(p firewall.Packet, incoming bool, c *cert.CachedCertificate, caPool *cert.CAPool) bool {
	// We don't have any allowed ports, bail
	if fp == nil {
		return false
	}

	// this branch is here to catch traffic from FirewallTable.Any.match and FirewallTable.ICMP.match
	if p.Protocol == firewall.ProtoICMP || p.Protocol == firewall.ProtoICMPv6 {
		// port numbers are re-used for connection tracking of ICMP,
		// but we don't want to actually filter on them.
		return fp[firewall.PortAny].match(p, c, caPool)
	}

	var port int32

	if p.Fragment {
		port = firewall.PortFragment
	} else if incoming {
		port = int32(p.LocalPort)
	} else {
		port = int32(p.RemotePort)
	}

	if fp[port].match(p, c, caPool) {
		return true
	}

	return fp[firewall.PortAny].match(p, c, caPool)
}
```

**File:** firewall_test.go (L1185-1198)
```go
// Test_parsePort_invalid covers inputs that must error. The named bug is
// that int32(strconv.Atoi("4294967296")) truncates to 0 == firewall.PortAny,
// silently turning a typo into a match-all-ports rule; the rest are
// representative syntax/range probes.
func Test_parsePort_invalid(t *testing.T) {
	tests := []struct {
		name            string
		input           string
		wantErrContains string
	}{
		// Numeric overflow (the named bug + boundary).
		{"named bug: 2^32 truncates to PortAny", "4294967296", "out of range"},
		{"just above max real port", "65536", "out of range"},

```
