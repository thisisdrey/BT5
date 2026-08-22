### Title
`PermitUDPPort` installs a subnet-unaware WFP PERMIT filter that unconditionally overrides host firewall (WDF) subnet restrictions on the nebula UDP listener port - ([File: wfp/wfp_windows.go])

### Summary
`PermitUDPPort` (enabled by default via `listen.windows_bypass_wdf`) installs WFP filters at `FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6` that match only `IP_PROTOCOL == UDP` and `IP_LOCAL_PORT == <port>`, with `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` and weight `0xFFFF` in a sublayer that arbitrates above Windows Defender Firewall's sublayer. There is no `FWPM_CONDITION_IP_REMOTE_ADDRESS` condition, so the permit applies to inbound UDP from any source address, unconditionally overriding any admin-configured WDF rule intended to scope the nebula port to a management subnet.

### Finding Description
`addUDPPortFilter` builds exactly two conditions - protocol and local port - with no remote-address/CIDR equivalent to nebula's own `local_cidr` concept in `firewall.go`'s `AddFirewallRulesFromConfig`/`FirewallRule.addRule` (which nebula uses to scope its own overlay firewall rules by `Cidr`/`LocalCidr`). [1](#0-0) 

The filter is explicitly constructed to win arbitration over any WDF (Windows Defender Firewall) rule at the same layer: it sets `fwpmFilterFlagClearActionRight` (`0x8`) and installs into a session-scoped sublayer with `weight: 0xFFFF`, and the package doc comment states this is intentional - "a high-weight permit at FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6 lets the matching inbound traffic through regardless of WDF rules." [2](#0-1) [3](#0-2) [4](#0-3) 

This is wired up by default: `bypassConn.ReloadConfig` calls `wfp.PermitUDPPort(addr.Port())` whenever `listen.windows_bypass_wdf` is true, which is the default. [5](#0-4) 

Consequently, an admin who configures a WDF rule restricting the nebula UDP port to a management subnet (e.g. via netsh/Group Policy) has that restriction silently bypassed at the WFP layer: any host on the network can send UDP to the bound port and it will reach the socket, because the higher-weight, clear-action-right filter permits it before WDF's rule is ever evaluated. Nebula's own certificate/handshake logic will still reject unauthenticated peers at the application layer, but the OS-level defense-in-depth control the admin relied on (restricting who can even reach the UDP socket) is defeated regardless of nebula-level firewall/`unsafe_routes` config.

### Impact Explanation
Any host-level WDF rule scoping the nebula UDP port to a specific subnet (a common defense-in-depth control, e.g. to reduce amplification/DoS/handshake-flood exposure or to satisfy compliance segmentation requirements) is fully and silently bypassed host-wide on Windows. This matches a firewall-bypass class impact: unauthenticated UDP packets from any address reach the nebula listener despite an administrator-configured restriction meant to prevent exactly that reachability.

### Likelihood Explanation
This triggers automatically and by default (`listen.windows_bypass_wdf` defaults to `true`) on every Windows nebula install, with no special attacker capability required beyond sending UDP packets from outside the intended subnet - it is not conditioned on any nebula-level firewall/`unsafe_routes` config and requires no privileged access or credentials from the attacker.

### Recommendation
Add remote-address scoping to `addUDPPortFilter` (an `FWPM_CONDITION_IP_REMOTE_ADDRESS`/`FWPM_CONDITION_IP_REMOTE_SUBNET`-style condition, configurable from `listen.windows_bypass_wdf` settings) so the WFP permit only widens exactly what nebula needs (e.g. its own overlay/lighthouse ranges), or clearly document that enabling this bypass forfeits all OS-level subnet ACLs and default it to `false` instead of `true`.

### Proof of Concept
Integration test plan (Windows, requires admin):
1. Configure a WDF rule via `netsh advfirewall firewall add rule ... localport=<nebula_port> protocol=UDP remoteip=<mgmt_subnet> action=allow` plus a default block for the port from other subnets.
2. Start nebula with default config (`listen.windows_bypass_wdf` unset/true), confirm `wfp.PermitUDPPort(port)` is installed (log line "Installed WFP filters bypassing Windows Defender Firewall...").
3. From a source address outside `<mgmt_subnet>`, send a UDP packet to the nebula port.
4. Assert the packet reaches the bound socket (e.g. via a packet counter or nebula's UDP receive metrics), demonstrating the WDF subnet restriction was bypassed.
5. Repeat with `listen.windows_bypass_wdf: false` and confirm the same out-of-subnet packet is now blocked by WDF, proving the WFP filter (not some other mechanism) was responsible for the bypass.

### Citations

**File:** wfp/wfp_windows.go (L5-10)
```go
// Package wfp installs Windows Filtering Platform (WFP) PERMIT filters in a dynamic, session-scoped sublayer.
// Because WFP sits below Windows Defender Firewall, a high-weight permit at FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6 lets
// the matching inbound traffic through regardless of WDF rules.
//
// Each Session owns its own engine handle. When the handle closes, every dynamic object added during the session
// is auto-deleted by Windows, so there are no orphaned filters.
```

**File:** wfp/wfp_windows.go (L76-79)
```go
// FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT prevents lower-priority filters in other sublayers,
// notably Windows Defender Firewall's MPSSVC_WF sublayer, which shares our 0xFFFF weight from overriding this PERMIT.
// Without it, a default WDF block at the same sublayer weight can still win arbitration.
const fwpmFilterFlagClearActionRight uint32 = 0x8
```

**File:** wfp/wfp_windows.go (L266-290)
```go
// registerSublayer adds a session-scoped sublayer with a freshly generated GUID, weight 0xFFFF so its filters arbitrate
// above WDF's default sublayer. The sublayer is dynamic (no PERSISTENT flag) and goes away when the engine handle closes.
func registerSublayer(engine uintptr) (windows.GUID, error) {
	key, err := windows.GenerateGUID()
	if err != nil {
		return windows.GUID{}, fmt.Errorf("GenerateGUID for sublayer: %w", err)
	}

	name, _ := windows.UTF16PtrFromString("Nebula WDF bypass sublayer")
	desc, _ := windows.UTF16PtrFromString("Permit filters bypassing Windows Defender Firewall")
	sl := fwpmSublayer0{
		subLayerKey: key,
		displayData: fwpmDisplayData0{name: name, description: desc},
		weight:      0xFFFF,
	}
	r1, _, _ := procFwpmSubLayerAdd0.Call(
		engine,
		uintptr(unsafe.Pointer(&sl)),
		0, // sd == NULL
	)
	if r1 != 0 {
		return windows.GUID{}, fmt.Errorf("FwpmSubLayerAdd0: 0x%x", r1)
	}
	return key, nil
}
```

**File:** wfp/wfp_windows.go (L331-354)
```go
// addUDPPortFilter installs a PERMIT filter that matches (IP_PROTOCOL == UDP) AND (IP_LOCAL_PORT == port).
// FWP_UINT8 and FWP_UINT16 are <= 32 bits so they live inline in the FWP_VALUE0 union.
func addUDPPortFilter(engine uintptr, sublayerKey, layer windows.GUID, port uint16) error {
	name, _ := windows.UTF16PtrFromString("Nebula allow UDP port inbound")
	desc, _ := windows.UTF16PtrFromString("Permits inbound UDP to a nebula listener port")

	conds := [2]fwpmFilterCondition0{
		{
			fieldKey:  fwpmConditionIPProtocol,
			matchType: fwpMatchEqual,
			conditionValue: fwpValue0{
				type_: fwpUint8,
				value: uintptr(ipprotoUDP),
			},
		},
		{
			fieldKey:  fwpmConditionIPLocalPort,
			matchType: fwpMatchEqual,
			conditionValue: fwpValue0{
				type_: fwpUint16,
				value: uintptr(port),
			},
		},
	}
```

**File:** udp/udp_bypass_windows.go (L29-49)
```go
func (b *bypassConn) ReloadConfig(c *config.C) {
	b.installOnce.Do(func() {
		if !c.GetBool("listen.windows_bypass_wdf", true) {
			return
		}
		addr, err := b.Conn.LocalAddr()
		if err != nil {
			b.l.Warn("Failed to query listener port for WFP bypass", "error", err)
			return
		}
		s, err := wfp.PermitUDPPort(addr.Port())
		if err != nil {
			b.l.Warn("Failed to install WFP bypass filters for listener", "error", err)
			return
		}
		b.l.Info("Installed WFP filters bypassing Windows Defender Firewall on UDP listener port",
			"port", addr.Port())
		b.session = s
	})
	b.Conn.ReloadConfig(c)
}
```
