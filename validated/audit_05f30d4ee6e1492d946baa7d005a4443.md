### Title
Windows Defender Firewall bypass installed by default for all inbound UDP/interface traffic - ([File: udp/udp_windows.go], [File: udp/udp_bypass_windows.go], [File: overlay/tun_windows.go], [File: wfp/wfp_windows.go])

### Summary
On Windows builds, Nebula unconditionally wraps every UDP listener and the wintun interface with code that installs Windows Filtering Platform (WFP) PERMIT filters that sit below, and override, Windows Defender Firewall (WDF). This is functionally the same bug class as the reported "emergency withdrawal" issue: production code that deliberately and by default disables a security-enforcement layer (there, on-chain withdrawal locks; here, the host firewall), reachable by any remote attacker sending traffic to the bypassed port/interface, with no certificate or authentication required to trigger the exposure.

### Finding Description
`NewListener` in `udp/udp_windows.go` calls `wrapWithWDFBypass(l, conn)` unconditionally on every UDP socket it creates, with no way to opt out at that call site: [1](#0-0) 

`wrapWithWDFBypass` installs a WFP PERMIT filter for the listener's UDP port the first time `ReloadConfig` runs, gated only by `listen.windows_bypass_wdf`, which **defaults to `true`**: [2](#0-1) 

Similarly, the tun/interface path installs an equivalent bypass for the wintun adapter, controlled by `tun.windows_bypass_wdf`, which also **defaults to `true`**: [3](#0-2) [4](#0-3) 

The actual bypass mechanism, in `wfp/wfp_windows.go`, explicitly documents that it places filters "below Windows Defender Firewall" using `FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6` with a PERMIT action and `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` set specifically so that WDF's own block rules at the same sublayer weight cannot override the permit: [5](#0-4) [6](#0-5) [7](#0-6) 

The port- and interface-scoped filter installers (`addUDPPortFilter`, `addInterfaceFilter`) confirm the effect: unconditional PERMIT for inbound UDP to the nebula port or inbound traffic on the nebula interface, regardless of any WDF inbound rule the administrator has configured: [8](#0-7) [9](#0-8) 

This matches the report's bug class exactly: intentional, hardcoded, default-active code whose entire purpose is to circumvent an enforcement mechanism (there, contract withdrawal locks controlled by a hardcoded `EMERGENCY_ADDR`; here, the OS firewall controlled by a hardcoded default-`true` bypass flag) that should not exist in a hardened production build, or at minimum should not be default-on.

### Impact Explanation
Because the bypass is active by default, any host administrator who relies on Windows Defender Firewall rules to restrict which remote sources may send UDP packets to the nebula listener port (or reach the nebula tun interface) will have those restrictions silently nullified. This is a firewall-enforcement bypass: unauthenticated remote UDP traffic to the nebula port — or arbitrary inbound traffic to the wintun interface's assigned IPs — is force-permitted by a higher-priority WFP filter no matter what WDF rules say, effectively reintroducing exposure the operator believed was blocked at the OS layer. Any Nebula packet-processing bugs (e.g., in header/handshake parsing) become directly reachable from network positions the firewall was supposed to exclude, since traffic is always allowed to hit the nebula socket/interface first.

### Likelihood Explanation
High. The bypass is installed automatically as part of normal startup on Windows (amd64/arm64, non-e2e builds) with no explicit administrator action required, since both `listen.windows_bypass_wdf` and `tun.windows_bypass_wdf` default to `true`. An attacker does not need a valid certificate, CA trust, or any handshake state — they only need network reachability to the host's UDP port or interface, which is exactly the condition WDF rules are normally used to restrict.

### Recommendation
Default `listen.windows_bypass_wdf` and `tun.windows_bypass_wdf` to `false`, requiring an explicit opt-in from the administrator, and clearly document in the config reference that enabling these options intentionally overrides Windows Defender Firewall inbound rules. Alternatively, remove the `FWPM_FILTER_FLAG_CLEAR_ACTION_RIGHT` override behavior so that existing WDF block rules retain the ability to take precedence, and re-test all affected UDP/tun code paths on Windows.

### Proof of Concept
1. Deploy Nebula on a Windows host with default configuration (no `listen.windows_bypass_wdf` or `tun.windows_bypass_wdf` set).
2. Configure Windows Defender Firewall with an inbound block rule for the Nebula UDP listener port (or for the nebula adapter).
3. Start Nebula; observe log line "Installed WFP filters bypassing Windows Defender Firewall on UDP listener port" / "...on nebula interface" from `wrapWithWDFBypass`/`installInterfaceBypass`. [10](#0-9) [11](#0-10) 
4. From a remote unauthenticated host, send a UDP packet to the nebula port (or traffic to the nebula interface's address). Despite the WDF block rule, the packet reaches the nebula socket/interface because the WFP PERMIT filter with `CLEAR_ACTION_RIGHT` outranks the WDF block at the same sublayer weight, confirming the firewall enforcement bypass.

### Citations

**File:** udp/udp_windows.go (L14-34)
```go
func NewListener(l *slog.Logger, ip netip.Addr, port int, multi bool, batch int) (Conn, error) {
	if multi {
		//NOTE: Technically we can support it with RIO but it wouldn't be at the socket level
		// The udp stack would need to be reworked to hide away the implementation differences between
		// Windows and Linux
		return nil, fmt.Errorf("multiple udp listeners not supported on windows")
	}

	var conn Conn
	rc, err := NewRIOListener(l, ip, port)
	if err == nil {
		conn = rc
	} else {
		l.Error("Falling back to standard udp sockets", "error", err)
		conn, err = NewGenericListener(l, ip, port, multi, batch)
		if err != nil {
			return nil, err
		}
	}
	return wrapWithWDFBypass(l, conn), nil
}
```

**File:** udp/udp_bypass_windows.go (L15-49)
```go
// wrapWithWDFBypass wraps a Conn so that the first ReloadConfig consults listen.windows_bypass_wdf
// and installs a WFP PERMIT filter for the listener's bound UDP port. The session is released when Close runs.
func wrapWithWDFBypass(l *slog.Logger, conn Conn) Conn {
	return &bypassConn{Conn: conn, l: l}
}

type bypassConn struct {
	Conn

	l           *slog.Logger
	installOnce sync.Once
	session     *wfp.Session
}

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

**File:** overlay/tun_windows.go (L71-80)
```go
	t := &winTun{
		Device:          deviceName,
		vpnNetworks:     vpnNetworks,
		MTU:             c.GetInt("tun.mtu", DefaultMTU),
		guid:            *guid,
		networkCategory: cat,
		setCategory:     setCat,
		bypassWDF:       c.GetBool("tun.windows_bypass_wdf", true),
		l:               l,
	}
```

**File:** overlay/tun_bypass_windows.go (L13-23)
```go
// installInterfaceBypass installs a WFP PERMIT filter scoped to the wintun interface LUID so inbound traffic on the
// nebula adapter bypasses Windows Defender Firewall.
func installInterfaceBypass(l *slog.Logger, luid uint64) closer {
	s, err := wfp.PermitInterface(luid)
	if err != nil {
		l.Warn("Failed to install WFP bypass filters on nebula interface", "error", err)
		return nil
	}
	l.Info("Installed WFP filters bypassing Windows Defender Firewall on nebula interface")
	return s
}
```

**File:** wfp/wfp_windows.go (L1-14)
```go
//go:build (amd64 || arm64) && !e2e_testing
// +build amd64 arm64
// +build !e2e_testing

// Package wfp installs Windows Filtering Platform (WFP) PERMIT filters in a dynamic, session-scoped sublayer.
// Because WFP sits below Windows Defender Firewall, a high-weight permit at FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6 lets
// the matching inbound traffic through regardless of WDF rules.
//
// Each Session owns its own engine handle. When the handle closes, every dynamic object added during the session
// is auto-deleted by Windows, so there are no orphaned filters.
//
// Type definitions and constants are derived from the wireguard-windows firewall package (MIT).
// Only the subset we exercise is reproduced.
package wfp
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

**File:** wfp/wfp_windows.go (L292-329)
```go
func addInterfaceFilter(engine uintptr, sublayerKey, layer windows.GUID, luid uint64) error {
	name, _ := windows.UTF16PtrFromString("Nebula allow interface inbound")
	desc, _ := windows.UTF16PtrFromString("Permits inbound traffic on a nebula interface")

	// luid must remain addressable through the syscall -- FWP_UINT64 is stored
	// by pointer in the FWP_VALUE0 union.
	cond := fwpmFilterCondition0{
		fieldKey:  fwpmConditionIPLocalInterface,
		matchType: fwpMatchEqual,
		conditionValue: fwpValue0{
			type_: fwpUint64,
			value: uintptr(unsafe.Pointer(&luid)),
		},
	}

	filter := fwpmFilter0{
		// filterKey left zero: WFP assigns one when the filter is added.
		displayData:         fwpmDisplayData0{name: name, description: desc},
		flags:               fwpmFilterFlagClearActionRight,
		layerKey:            layer,
		subLayerKey:         sublayerKey,
		weight:              fwpValue0{type_: fwpUint8, value: uintptr(15)},
		numFilterConditions: 1,
		filterCondition:     &cond,
		action:              fwpmAction0{actionType: fwpActionPermit},
	}

	r1, _, _ := procFwpmFilterAdd0.Call(
		engine,
		uintptr(unsafe.Pointer(&filter)),
		0, // sd == NULL
		0, // id == NULL
	)
	if r1 != 0 {
		return fmt.Errorf("FwpmFilterAdd0: 0x%x", r1)
	}
	return nil
}
```

**File:** wfp/wfp_windows.go (L331-376)
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

	filter := fwpmFilter0{
		displayData:         fwpmDisplayData0{name: name, description: desc},
		flags:               fwpmFilterFlagClearActionRight,
		layerKey:            layer,
		subLayerKey:         sublayerKey,
		weight:              fwpValue0{type_: fwpUint8, value: uintptr(15)},
		numFilterConditions: 2,
		filterCondition:     &conds[0],
		action:              fwpmAction0{actionType: fwpActionPermit},
	}

	r1, _, _ := procFwpmFilterAdd0.Call(
		engine,
		uintptr(unsafe.Pointer(&filter)),
		0,
		0,
	)
	if r1 != 0 {
		return fmt.Errorf("FwpmFilterAdd0: 0x%x", r1)
	}
	return nil
```
