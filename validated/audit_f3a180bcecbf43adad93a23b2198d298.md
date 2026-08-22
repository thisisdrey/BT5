Based on my research, I found a concrete analog matching the bug class described in the report: a protective/config-toggle mechanism that installs a bypass but never tears it down when the operator later tries to disable it, leaving a persistent, remotely-exploitable state — directly matching "firewall bypass" impact.

### Title
Windows WFP WDF-Bypass Filter Is Installed Once and Never Removed on Config Reload, Permanently Bypassing Windows Defender Firewall - (File: `udp/udp_bypass_windows.go`)

### Summary
`bypassConn.ReloadConfig` uses a `sync.Once` to install a WFP PERMIT filter for the Nebula UDP listener port, guarded by `listen.windows_bypass_wdf`. Once installed, the filter session is held for the lifetime of the connection and is only closed in `Close()`. There is no code path that re-evaluates `listen.windows_bypass_wdf` on subsequent reloads, nor any path that tears down the WFP session if the option is later set to `false`. This mirrors the `exitShelter` bug class: a state-mutating action (`enterShelter`/install-bypass) is not properly reversible by the corresponding restore action (`exitShelter`/disable-bypass), leaving the system permanently in the "unsafe" state while the operator believes it has been restored to a safe one.

### Finding Description
`wrapWithWDFBypass` wraps the UDP `Conn` so that the first call to `ReloadConfig` checks `listen.windows_bypass_wdf` (default `true`) and, if set, calls `wfp.PermitUDPPort` to install a PERMIT filter at `FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6`. This filter sits below Windows Defender Firewall (WDF), so matching inbound UDP traffic bypasses WDF entirely: [1](#0-0) 

The install is gated by `installOnce.Do(...)`, so it only ever runs on the *first* `ReloadConfig` call. Any later config reload that flips `listen.windows_bypass_wdf` to `false` (an operator explicitly trying to restore WDF control, analogous to calling `exitShelter`) is silently ignored — the closure body never re-executes, so the session is neither closed nor re-evaluated: [2](#0-1) 

The only place the session is released is `Close()`, which only runs on process shutdown, not on config reload: [3](#0-2) 

This is the direct analog of the `exitShelter` bug: `enterShelter` (installing the WFP bypass) mutates protective state, but the "exit"/undo action (disabling `windows_bypass_wdf` via reload) never restores the pre-bypass state (removing the WFP session), leaving the resource (the firewall's protective boundary) permanently in the bypassed condition regardless of the administrator's later configuration intent.

### Impact Explanation
This falls squarely into the "firewall bypass" impact category. An operator who deploys Nebula with the default `listen.windows_bypass_wdf: true`, discovers the WDF-bypass behavior is undesirable, and disables it via `listen.windows_bypass_wdf: false` followed by a SIGHUP/config reload will believe WDF protections on the listener port have been restored — but they have not. Any remote attacker on the underlay network can continue to send UDP traffic to the Nebula listener port and have it pass the WFP PERMIT filter, bypassing any WDF inbound rules the operator relies on, with no cryptographic material or valid certificate required to reach this surface (WFP evaluation happens before any Nebula-layer authentication).

### Likelihood Explanation
Any Windows deployment that (a) starts with the bypass enabled (the default), and (b) later disables it via config reload without a full process restart will silently retain the bypass. Given `windows_bypass_wdf` defaults to `true` and reload-without-restart (SIGHUP) is a first-class supported Nebula workflow, this is easily triggered through ordinary administration.

### Recommendation
Remove the `sync.Once` gate and instead track the current desired state; on each `ReloadConfig` call, compare the new `listen.windows_bypass_wdf` value against the currently installed state and either install a new WFP session (if turning on) or call `session.Close()` to remove it (if turning off), so the live bypass state always reflects the latest configuration rather than only the first reload's value.

### Proof of Concept
1. Start Nebula on Windows with default config (`listen.windows_bypass_wdf` unset → defaults to `true`). `ReloadConfig` fires on startup and `PermitUDPPort` installs the WFP PERMIT filter for the listener port, per [4](#0-3) .
2. Confirm via `netsh wfp show state` or the WFP filter list that a dynamic PERMIT filter exists for the listener UDP port at `FWPM_LAYER_ALE_AUTH_RECV_ACCEPT_V4/V6`.
3. Edit the config to set `listen.windows_bypass_wdf: false` and send SIGHUP (or use the `reload` SSH command) to trigger `ReloadConfig` again.
4. Observe that `installOnce.Do` does not re-run its closure (per `sync.Once` semantics), so `PermitUDPPort`'s session is neither closed nor reinstalled — the WFP PERMIT filter from step 1 remains active.
5. From an external host, add a WDF inbound block rule for the listener UDP port and confirm that traffic to that port still succeeds, proving that Windows Defender Firewall is still being bypassed despite the administrator having disabled `windows_bypass_wdf`.

### Citations

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

**File:** udp/udp_bypass_windows.go (L51-57)
```go
func (b *bypassConn) Close() error {
	if b.session != nil {
		b.session.Close()
		b.session = nil
	}
	return b.Conn.Close()
}
```
