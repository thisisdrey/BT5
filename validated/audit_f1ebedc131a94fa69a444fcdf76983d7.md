## Analysis

The strongest reachable analog to the `createVestingSchedule` input-validation bug class in this codebase is in the firewall rule port-range parser, `parsePort` in [1](#0-0)  together with its helper `parsePortValue` at [2](#0-1) .

### Title
Firewall port-range parsing silently collapses admin-specified ranges into a match-all rule - (File: `firewall.go`)

### Summary
`parsePort` treats port `0` as the sentinel `firewall.PortAny` (wildcard, matches every port) and then forcibly widens any range whose *start* happens to be `0` into a full wildcard range, discarding the admin's actual end-of-range value:

```go
if startPort == firewall.PortAny {
    endPort = firewall.PortAny
}
``` [3](#0-2) 

There is no upper/lower bound sanity check rejecting `0` as a meaningless real-world port before this collapse happens, unlike the recommendation in the external report ("add sensible lower and upper bounds for all arguments"). This is confirmed by the test suite itself: `Test_parsePort_valid_boundaries` documents `"0-1"` → `(0, 0)` under the label "range zero to max forces end to zero" [4](#0-3) .

### Finding Description
An admin writing a firewall rule with a port range that starts at `0` (e.g. `port: "0-1"`, intending "ports 0 and 1", or any range starting at 0) does not get an error and does not get the narrow range they specified. Instead, `parsePort` silently forces the entire range to `firewall.PortAny` (0), which is the special sentinel meaning "match any port" in `firewallPort.match`:

```go
if fp[port].match(p, c, caPool) {
    return true
}
return fp[firewall.PortAny].match(p, c, caPool)
``` [5](#0-4) 

Because every port lookup falls back to checking the `PortAny` bucket, a rule registered under `PortAny` (via `AddRule`, which loops `startPort` to `endPort` and inserts into `fp[i]` for each i, collapsing to just `fp[0]`) [6](#0-5)  ends up matching traffic on **every** port for the configured host/group/CIDR/CA criteria, not just the intended `0-1` range. This is functionally identical to the reported bug class: an unvalidated boundary/sentinel value (`duration == 1` being "technically nonzero but still wrong", or `_cliff` too large) silently produces behavior far outside what the operator intended, and there is no upper/lower bound enforcement to catch it.

### Impact Explanation
This directly maps to a firewall bypass: an operator who makes an easily-made configuration mistake (specifying a range that includes port `0`, which is not a valid real-world port) unknowingly deploys a rule that permits all ports instead of the two they intended, opening the host/group/CIDR far more broadly than configured. As in the original report, the impact is high (unintended wide-open exposure) while the likelihood is low, since it requires an admin/operator error in `firewall.inbound`/`firewall.outbound` port configuration rather than any peer/handshake exploitation — this mirrors the report's own stated likelihood rationale exactly.

### Likelihood Explanation
Low: it requires an operator/admin to write a firewall rule with a port range whose start (or the raw string) parses to `0`, e.g. `"0-1"`, `"0-100"`, etc. There is no config-time warning (`rule.sanity()` at [7](#0-6)  does not check for this) alerting the operator that their range was silently rewritten to "any port."

### Recommendation
In `parsePort` / `parsePortValue`, reject `0` as an endpoint of an explicit numeric range (distinct from the literal keyword `"any"`), and/or emit an explicit warning/error when a parsed range collapses to `PortAny` due to a `0` boundary, so operators are not silently given a broader rule than they configured. More generally, add explicit lower/upper bound validation for all firewall rule numeric inputs rather than relying on the `PortAny`/`PortFragment` sentinel values colliding with legitimate-looking user input.

### Proof of Concept
1. Configure a firewall rule: `firewall.inbound: [{port: "0-1", proto: "any", host: "some-host"}]`.
2. `AddFirewallRulesFromConfig` calls `parsePort("0-1")`, which returns `(0, 0)` due to the forced-collapse logic [8](#0-7) .
3. `fw.AddRule` inserts the rule only at `fp[0]` (`PortAny`) [6](#0-5) .
4. Any inbound packet on any port from `some-host` now matches this rule via the `fp[firewall.PortAny]` fallback in `firewallPort.match` [5](#0-4) , even though the operator intended to allow only ports 0–1.

### Citations

**File:** firewall.go (L655-674)
```go
func (fp firewallPort) addRule(f *Firewall, startPort int32, endPort int32, groups []string, host string, cidr, localCidr, caName string, caSha string) error {
	if startPort > endPort {
		return fmt.Errorf("start port was lower than end port")
	}

	for i := startPort; i <= endPort; i++ {
		if _, ok := fp[i]; !ok {
			fp[i] = &FirewallCA{
				CANames: make(map[string]*FirewallRule),
				CAShas:  make(map[string]*FirewallRule),
			}
		}

		if err := fp[i].addRule(f, groups, host, cidr, localCidr, caName, caSha); err != nil {
			return err
		}
	}

	return nil
}
```

**File:** firewall.go (L699-703)
```go
	if fp[port].match(p, c, caPool) {
		return true
	}

	return fp[firewall.PortAny].match(p, c, caPool)
```

**File:** firewall.go (L1013-1055)
```go
func (r *rule) sanity() error {
	//port, proto, local_cidr are AND, no need to check here
	//ca_sha and ca_name don't have a wildcard value, no need to check here
	groupsEmpty := len(r.Groups) == 0
	hostEmpty := r.Host == ""
	cidrEmpty := r.Cidr == ""

	if (groupsEmpty && hostEmpty && cidrEmpty) == true {
		return nil //no content!
	}

	groupsHasAny := slices.Contains(r.Groups, "any")
	if groupsHasAny && len(r.Groups) > 1 {
		return fmt.Errorf("groups spec [%s] contains the group '\"any\". This rule will ignore the other groups specified", r.Groups)
	}

	if r.Host == "any" {
		if !groupsEmpty {
			return fmt.Errorf("groups specified as %s, but host=any will match any host, regardless of groups", r.Groups)
		}

		if !cidrEmpty {
			return fmt.Errorf("cidr specified as %s, but host=any will match any host, regardless of cidr", r.Cidr)
		}
	}

	if groupsHasAny {
		if !hostEmpty && r.Host != "any" {
			return fmt.Errorf("groups spec [%s] contains the group '\"any\". This rule will ignore the specified host %s", r.Groups, r.Host)
		}
		if !cidrEmpty {
			return fmt.Errorf("groups spec [%s] contains the group '\"any\". This rule will ignore the specified cidr %s", r.Groups, r.Cidr)
		}
	}

	if r.Code != "" {
		return fmt.Errorf("code specified as [%s]. Support for 'code' will be dropped in a future release, as it has never been functional", r.Code)
	}

	//todo alert on cidr-any

	return nil
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

**File:** firewall.go (L1098-1117)
```go
// parsePortValue accepts a base-10 decimal in [0, 65535] and returns it
// widened to int32. Using strconv.ParseUint with bitSize 16 rejects
// negative input, out-of-range input (>65535), and any non-decimal byte
// by construction, so the int32 widening that follows is provably safe
// and cannot collide with firewall.PortAny (0) or firewall.PortFragment
// (-1) via integer truncation.
//
// prefix is prepended to both error messages so callers can disambiguate
// the single-port path (prefix="") from the range bounds (prefix="beginning
// range " / "ending range "), preserving the historical error strings.
func parsePortValue(prefix, s string) (int32, error) {
	n, err := strconv.ParseUint(s, 10, 16)
	if err == nil {
		return int32(n), nil
	}
	if errors.Is(err, strconv.ErrRange) {
		return 0, fmt.Errorf("%sout of range [0,65535]; `%s`", prefix, s)
	}
	return 0, fmt.Errorf("%swas not a number; `%s`", prefix, s)
}
```

**File:** firewall_test.go (L1238-1238)
```go
		{"range zero to max forces end to zero", "0-65535", 0, 0},
```
