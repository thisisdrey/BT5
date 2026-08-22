### Title
Unvalidated `handshakes.retries` / `handshakes.try_interval` config allows a zero-retry setting to make outbound handshake completion always fail on packet loss - ([File: handshake_manager.go])

### Summary
`HandshakeConfig.retries` and `HandshakeConfig.tryInterval` are read directly from user config with no lower-bound validation (`retries=0` or `try_interval=0` are accepted as-is), exactly mirroring the `maxGas` class of bug: an unchecked "how much work am I allowed to do to complete this operation" parameter that, at low or zero values, causes the associated state machine to skip work or abort immediately rather than complete/retry.

### Finding Description
`handshakeConfig` is populated straight from config values with no minimum enforced: [1](#0-0) 

In `handleOutbound`, the very first thing checked on every timer tick is whether the attempt counter has reached `retries`: [2](#0-1) 

Since `hh.counter` starts at `0` and the check is `hh.counter >= hm.config.retries`, setting `handshakes.retries` to `0` causes `handleOutbound` to delete the pending `HandshakeHostInfo` and log a timeout on the very first outbound tick — no handshake packet is ever sent. This is the direct analog of "`maxGas = 0` will make the process skip the loop entirely": the operator-controlled retry-budget parameter has a valid-looking value (`0`) that silently disables the entire handshake-initiation path.

Symmetrically, `hsTimeout(retries, tryInterval)` is used to size the wheel duration for the timer wheel driving retries: [3](#0-2) 
If `retries` or `tryInterval` is small/zero, this computed timeout collapses toward zero, and `NewLockingTimerWheel`/`NewTimerWheel` compute wheel length as `(max/min)+2`: [4](#0-3) 
An externally-triggered handshake attempt (any unauthenticated peer sending traffic that causes `StartHandshake` to run) is queued into this wheel via `OutboundHandshakeTimer.Add`: [5](#0-4) 
so degenerate `min`/`max` values driven by misconfigured `retries`/`try_interval` affect the availability of the handshake subsystem for every incoming handshake attempt, not just administrator-initiated ones.

There is no floor/ceiling validation anywhere in the config-loading path for these values (confirmed by searching for any `retries <= 0`, `retries == 0`, `tryInterval <= 0`, `tryInterval == 0` guard — none exist in the codebase).

### Impact Explanation
If `handshakes.retries` is configured to `0` (a value the config loader accepts without error), the node will never actually transmit a stage-0 handshake packet for any outbound handshake attempt — `handleOutbound` immediately hits the "out of time" branch and deletes the pending hostinfo before `hm.outside.WriteTo(stage0, addr)` is ever reached. This makes the node functionally unable to establish new tunnels to any peer (a persistent, low-effort denial of service of the handshake subsystem), exactly analogous to the reported `maxGas = 0` "skip the loop entirely" failure mode. Conversely, a very large `retries`/`try_interval` combination can keep exhausted or long-abandoned handshake attempts occupying `vpnIps`/timer-wheel state far longer than intended, degrading handshake-manager memory/CPU usage under churn.

### Likelihood Explanation
This requires an operator/administrator to set `handshakes.retries: 0` (or an equivalently degenerate `try_interval`) in the config, similar to how the original Morpho finding required a badly chosen `maxGas` value set by governance. It is not attacker-triggerable on its own, but once misconfigured, any peer (including unauthenticated attackers sending unsolicited traffic that provokes `StartHandshake`) is affected by the resulting failure mode network-wide, matching the report's own framing that this is a parameter-safety issue rather than an attacker-forged-value issue.

### Recommendation
Add validation when loading `handshakes.retries` and `handshakes.try_interval` (and derived `handshakes.trigger_buffer`) in `main.go`, rejecting or clamping to a safe minimum (e.g., `retries >= 1`, `try_interval` above the timer-wheel's practical tick resolution) with a fatal config error, similar to the `tun.unsafe_routes` MTU floor check already present elsewhere in the codebase: [6](#0-5) 

### Proof of Concept
1. Deploy a nebula node with config:
```yaml
handshakes:
  retries: 0
  try_interval: 100ms
```
2. Trigger an outbound handshake (e.g., send a tun packet destined for an unconnected peer, or have any peer contact this node causing `StartHandshake`/`GetOrHandshake` to run).
3. Observe via logs/metrics that `handshake_manager.timed_out` increments immediately and no stage-0 handshake packet is ever placed on the wire — confirmed by the code path in `handleOutbound` where `hh.counter (0) >= hm.config.retries (0)` is true on the very first tick, before `hm.outside.WriteTo` is reached: [7](#0-6)

### Citations

**File:** main.go (L198-203)
```go
	handshakeConfig := HandshakeConfig{
		tryInterval:    c.GetDuration("handshakes.try_interval", DefaultHandshakeTryInterval),
		retries:        int64(c.GetInt("handshakes.retries", DefaultHandshakeRetries)),
		triggerBuffer:  c.GetInt("handshakes.trigger_buffer", DefaultHandshakeTriggerBuffer),
		messageMetrics: messageMetrics,
	}
```

**File:** handshake_manager.go (L207-247)
```go
func (hm *HandshakeManager) handleOutbound(vpnIp netip.Addr, lighthouseTriggered bool) {
	hh := hm.queryVpnIp(vpnIp)
	if hh == nil {
		return
	}
	hh.Lock()
	defer hh.Unlock()

	hostinfo := hh.hostinfo
	// If we are out of time, clean up
	if hh.counter >= hm.config.retries {
		fields := []any{
			"udpAddrs", hh.hostinfo.remotes.CopyAddrs(hm.mainHostMap.GetPreferredRanges()),
			"initiatorIndex", hh.hostinfo.localIndexId,
			"durationNs", time.Since(hh.startTime).Nanoseconds(),
		}
		// hh.machine can be nil here if buildStage0Packet never succeeded
		// (e.g., no certificate available). In that case there's no useful
		// handshake metadata to log.
		if hh.machine != nil {
			fields = append(fields, "handshake", m{
				"stage": uint64(hh.machine.MessageIndex()),
				"style": header.SubTypeName(header.Handshake, hh.machine.Subtype()),
			})
		}
		hh.hostinfo.logger(hm.l).Info("Handshake timed out", fields...)
		hm.metricTimedOut.Inc(1)
		hm.DeleteHostInfo(hostinfo)
		return
	}

	// Increment the counter to increase our delay, linear backoff
	hh.counter++

	// Check if we have a handshake packet to transmit yet
	if !hh.ready {
		if !hm.buildStage0Packet(hh) {
			hm.OutboundHandshakeTimer.Add(vpnIp, hm.config.tryInterval*time.Duration(hh.counter))
			return
		}
	}
```

**File:** handshake_manager.go (L381-387)
```go
	hh := &HandshakeHostInfo{
		hostinfo:  hostinfo,
		startTime: time.Now(),
	}
	hm.vpnIps[vpnAddr] = hh
	hm.metricInitiated.Inc(1)
	hm.OutboundHandshakeTimer.Add(vpnAddr, hm.config.tryInterval)
```

**File:** handshake_manager.go (L645-647)
```go
func hsTimeout(tries int64, interval time.Duration) time.Duration {
	return time.Duration(tries / 2 * ((2 * int64(interval)) + (tries-1)*int64(interval)))
}
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

**File:** overlay/route.go (L173-186)
```go
		var mtu int
		if rMtu, ok := m["mtu"]; ok {
			mtu, ok = rMtu.(int)
			if !ok {
				mtu, err = strconv.Atoi(rMtu.(string))
				if err != nil {
					return nil, fmt.Errorf("entry %v.mtu in tun.unsafe_routes is not an integer: %v", i+1, err)
				}
			}

			if mtu != 0 && mtu < 500 {
				return nil, fmt.Errorf("entry %v.mtu in tun.unsafe_routes is below 500: %v", i+1, mtu)
			}
		}
```
