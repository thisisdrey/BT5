### Title
Relay entry deleted on failed lookup without checking if it was the peer's only relay path, permanently stranding the tunnel - (File: inside.go)

### Summary
The disclosed bug describes a case where a resource (bond interest) is consumed/distributed without first checking whether any recipient (bond supply) still exists, leaving the value permanently stuck. The same bug class is reachable in Nebula's relay-forwarding path in `Interface.sendNoMetrics()`: when a packet must be relayed because no direct remote is known, the code walks the hostinfo's relay list and deletes a relay entry the moment its lookup fails — without checking whether it was the peer's only remaining relay path or triggering any recovery action.

### Finding Description
`Interface.sendNoMetrics` falls back to relaying when neither an explicit `remote` nor `hostinfo.GetRemote()` is valid: [1](#0-0) 

For each relay IP recorded on `hostinfo.relayState`, it calls `f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relayIP)`. If that lookup returns an error (the relay's own `HostInfo` no longer exists in the hostmap — e.g., its tunnel was torn down and `unlockedDeleteHostInfo` already ran), the code unconditionally calls `hostinfo.relayState.DeleteRelay(relayIP)` and moves to the next candidate. There is no check for whether this was the peer's only relay, and no fallback such as re-querying the lighthouse or triggering a fresh handshake/relay-establishment attempt is fired from this path.

This mirrors the reported root cause pattern exactly: a "withdraw"/"consume" operation (`DeleteRelay`) executes without first verifying there is remaining "supply" (another relay, or any mechanism to re-establish one) to keep the flow alive, so the state silently degrades to unrecoverable.

Contrast this with the code paths that do handle the equivalent situation correctly — e.g. `unlockedDeleteHostInfo`, which explicitly calls `unlockedDisestablishVpnAddrRelayFor` to mark dependent relay state so it gets recreated later: [2](#0-1) 

and the relay-request handler which re-establishes a `Disestablished` relay rather than tearing it down: [3](#0-2) 

The e2e regression test `TestRelayHandshakeOverDisestablishedEntry` documents this exact bug class ("its first transmit deletes its only relay and the tunnel is born transmit-dead: them can receive but every send is silently dropped") for the `handleCreateRelayRequest` path: [4](#0-3) 

but the equivalent hardening was not applied to the `sendNoMetrics` relay-selection loop in `inside.go`, which still deletes the only relay entry on a stale-lookup failure with no compensating re-establishment trigger.

### Impact Explanation
When a peer is only reachable via relay and the relay hostinfo has been torn down/evicted (a routine occurrence — connection manager traffic-check timeouts, `CloseTunnel`, hostmap eviction via `MaxHostInfosPerVpnIp`), the very next packet destined for that peer removes the (only) relay entry from `relayState` instead of retrying it. Since nothing else re-populates `relayState.relays` for that hostinfo proactively, every subsequent outbound packet to the peer is silently dropped at the `sendNoMetrics` relay loop — the tunnel is effectively transmit-dead until an external event (e.g., a fresh inbound handshake or explicit `Handshake()` call elsewhere) rebuilds relay state. This is a remote state-poisoning / availability impact: legitimate traffic is silently and durably dropped with no error surfaced to the application layer.

### Likelihood Explanation
No attacker action or forged certificate is required. Relay hostinfo teardown happens naturally through connection-manager idle timeouts, tunnel closes, or hostinfo eviction under `MaxHostInfosPerVpnIp`. Any node relying on a single relay to reach a peer will hit this path once that relay's local `HostInfo` disappears before the dependent node's relay state catches up — a routine, non-adversarial race that is reachable purely through normal churn in hostmap/relay state.

### Recommendation
Before calling `hostinfo.relayState.DeleteRelay(relayIP)` on a failed `QueryVpnAddrsRelayFor` lookup, check whether this is the last relay for `hostinfo` and, if so, trigger relay re-discovery (e.g., invoke the same logic `relayManager.StartRelays` uses, or queue a fresh `Handshake`/lighthouse query) instead of only logging and dropping the packet. More generally, any relay/entry removal in the hot send path should be paired with a corresponding re-establishment trigger, mirroring what `unlockedDisestablishVpnAddrRelayFor` already does for hostmap-level teardown.

### Proof of Concept
1. Node `me` uses relay `R` to reach `them` (no direct remote known), with `relayState.relays == [R]`.
2. `R`'s `HostInfo` on `me`'s node is torn down (connection-manager traffic timeout, `CloseTunnel`, or `MaxHostInfosPerVpnIp` eviction) while `me`'s hostinfo for `them` still lists `R` in `relayState`.
3. `me` sends a tunnel packet to `them`. `sendNoMetrics` enters the relay branch, calls `QueryVpnAddrsRelayFor`, which fails because `R`'s hostinfo is gone.
4. `hostinfo.relayState.DeleteRelay(R)` executes, removing the only relay; the function returns without sending, and without triggering any relay re-establishment.
5. Every subsequent packet to `them` silently drops in the same branch (empty relay list), leaving the tunnel permanently transmit-dead until an unrelated external event re-populates relay state.

### Citations

**File:** inside.go (L414-429)
```go
	} else {
		// Try to send via a relay
		for _, relayIP := range hostinfo.relayState.CopyRelayIps() {
			relayHostInfo, relay, err := f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relayIP)
			if err != nil {
				hostinfo.relayState.DeleteRelay(relayIP)
				hostinfo.logger(f.l).Info("sendNoMetrics failed to find HostInfo",
					"relay", relayIP,
					"error", err,
				)
				continue
			}
			f.SendVia(relayHostInfo, relay, out, nb, fullOut[:header.Len+len(out)], true)
			break
		}
	}
```

**File:** hostmap.go (L533-537)
```go
	if final {
		// I have lost connectivity to my peers. My relay tunnel is likely broken. Mark the next
		// hops as 'Requested' so that new relay tunnels are created in the future.
		hm.unlockedDisestablishVpnAddrRelayFor(hostinfo)
	}
```

**File:** relay_manager.go (L465-474)
```go
			case Disestablished:
				if existingRelay.RemoteIndex != m.InitiatorRelayIndex {
					// We got a brand new Relay request, because its index is different than what we saw before.
					// This should never happen. The peer should never change an index, once created.
					logMsg.Error("Existing relay mismatch with CreateRelayRequest",
						"existingRemoteIndex", existingRelay.RemoteIndex)
					return
				}
				// Mark the relay as 'Established' because it's safe to use again
				h.relayState.UpdateRelayForByIpState(from, Established)
```

**File:** e2e/handshakes_test.go (L728-734)
```go
func TestRelayHandshakeOverDisestablishedEntry(t *testing.T) {
	t.Parallel()
	// If them tears down the tunnel while me keeps Established relay state, me's next
	// handshake flows through the relay with no fresh CreateRelayRequest and lands on
	// them's Disestablished terminal relay entry. them must re-establish that entry, or
	// its first transmit deletes its only relay and the tunnel is born transmit-dead:
	// them can receive but every send is silently dropped.
```
