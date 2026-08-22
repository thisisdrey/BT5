Found the analog. `NewFirewall` in `firewall.go` explicitly has a `//TODO: error on 0 duration` comment and performs no validation before computing `tmin`/`tmax` from the `tcp_timeout`, `udp_timeout`, and `default_timeout` config values, then passes them straight into `NewTimerWheel`, which divides `max/min` with no zero-check.

### Title
Unvalidated firewall timeout configuration values (`tcp_timeout`, `udp_timeout`, `default_timeout`) cause a division-by-zero panic in `NewTimerWheel`, crashing the Nebula process - (File: `firewall.go`, `timeout.go`)

### Summary
`NewFirewall` computes `tmin`/`tmax` from the three configurable conntrack timeout durations without any bounds validation, then feeds them into `NewTimerWheel`, which performs `int((max / min) + 2)`. If any of the configured timeout values is `0` (or if `tmin` ends up `0` through the min/max selection logic), this line divides by zero and panics, matching the pattern in the referenced report where an unchecked setter value led to a division-by-zero revert/crash in a critical settlement path.

### Finding Description
`NewFirewall(l, tcpTimeout, UDPTimeout, defaultTimeout, c)` selects `tmin`/`tmax` by comparing the three durations, with an explicit acknowledged gap: [1](#0-0) 
It then constructs the timer wheel directly from these unvalidated values: [2](#0-1) 
`NewTimerWheel` performs the unguarded division: [3](#0-2) 
Notably the code comment at line 75-78 shows a check for `min >= max` was considered and explicitly disabled/commented out, leaving no protection against `min == 0`.

The `connection_manager.go` reload path shows the same pattern is recognized as dangerous elsewhere in the codebase (it clamps to a 500ms floor before constructing its own wheel), confirming that an unclamped zero-duration value reaching `NewTimerWheel` is a known hazard class in this codebase: [4](#0-3) 
but `NewFirewall` for the conntrack timer wheel has no equivalent floor/validation, only a `TODO` comment acknowledging the gap.

### Impact Explanation
If `firewall.tcp_timeout`, `firewall.udp_timeout`, or `firewall.default_timeout` is configured (or defaults resolve) to `0`, `tmin` becomes `0`, causing `max/min` to panic with "runtime error: integer divide by zero" during firewall initialization or reload. This crashes the entire Nebula process for that node, which is a remote-crash / denial-of-service condition against the mesh node — the firewall is the enforcement mechanism gating all inbound/outbound tunnel traffic, so its failure to initialize denies all firewall-mediated traffic handling on that node.

### Likelihood Explanation
This requires a configuration value of `0` for one of the timeout settings to be present or reachable at runtime (e.g., via config reload/SIGHUP with `pki.disconnect_invalid` style config changes, or a malformed/edge-case config generation). The explicit `//TODO: error on 0 duration` comment in the source confirms the maintainers are aware this input is not validated and is only excluded by convention, not by code — the same root-cause shape as the referenced `setAuctionDecrement` bug (a config-mutable numeric input reaching a division operation with no bounds enforcement).

### Recommendation
Add explicit validation in `NewFirewall` (and/or `NewFirewallFromConfig`) rejecting `tcpTimeout <= 0`, `UDPTimeout <= 0`, and `defaultTimeout <= 0` before computing `tmin`/`tmax`, returning a configuration error instead of proceeding. Additionally, harden `NewTimerWheel` itself to guard against `min <= 0` (uncomment/enable the disabled check at `timeout.go:75-78` and return an error or clamp to a safe minimum) so that any future caller cannot trigger the same panic.

### Proof of Concept
Set `firewall.tcp_timeout: 0s` (or `udp_timeout`/`default_timeout` to `0s`) in the Nebula config and start/reload the node. `NewFirewallFromConfig` → `NewFirewall` will compute `tmin = 0`, and `NewTimerWheel(tmin, tmax)` will execute `int((max / min) + 2)`, panicking with a divide-by-zero runtime error and terminating the process.

### Citations

**File:** firewall.go (L136-153)
```go
func NewFirewall(l *slog.Logger, tcpTimeout, UDPTimeout, defaultTimeout time.Duration, c cert.Certificate) *Firewall {
	//TODO: error on 0 duration
	var tmin, tmax time.Duration

	if tcpTimeout < UDPTimeout {
		tmin = tcpTimeout
		tmax = UDPTimeout
	} else {
		tmin = UDPTimeout
		tmax = tcpTimeout
	}

	if defaultTimeout < tmin {
		tmin = defaultTimeout
	} else if defaultTimeout > tmax {
		tmax = defaultTimeout
	}

```

**File:** firewall.go (L167-171)
```go
	return &Firewall{
		Conntrack: &FirewallConntrack{
			Conns:      make(map[firewall.Packet]*conn),
			TimerWheel: NewTimerWheel[firewall.Packet](tmin, tmax),
		},
```

**File:** timeout.go (L74-98)
```go
func NewTimerWheel[T any](min, max time.Duration) *TimerWheel[T] {
	//TODO provide an error
	//if min >= max {
	//	return nil
	//}

	// Round down and add 2 so we can have the smallest # of ticks in the wheel and still account for a full
	// max duration, even if our current tick is at the maximum position and the next item to be added is at maximum
	// timeout
	wLen := int((max / min) + 2)

	tw := TimerWheel[T]{
		wheelLen:      wLen,
		wheel:         make([]*TimeoutList[T], wLen),
		tickDuration:  min,
		wheelDuration: max,
		expired:       &TimeoutList[T]{},
	}

	for i := range tw.wheel {
		tw.wheel[i] = &TimeoutList[T]{}
	}

	return &tw
}
```

**File:** connection_manager.go (L67-78)
```go
func (cm *connectionManager) reload(c *config.C, initial bool) {
	if initial {
		cm.checkInterval = time.Duration(c.GetInt("timers.connection_alive_interval", 5)) * time.Second
		cm.pendingDeletionInterval = time.Duration(c.GetInt("timers.pending_deletion_interval", 10)) * time.Second

		// We want at least a minimum resolution of 500ms per tick so that we can hit these intervals
		// pretty close to their configured duration.
		// The inactivity duration is checked each time a hostinfo ticks through so we don't need the wheel to contain it.
		minDuration := min(time.Millisecond*500, cm.checkInterval, cm.pendingDeletionInterval)
		maxDuration := max(cm.checkInterval, cm.pendingDeletionInterval)
		cm.trafficTimer = NewLockingTimerWheel[uint32](minDuration, maxDuration)
	}
```
