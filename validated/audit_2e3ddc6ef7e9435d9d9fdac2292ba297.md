Given the report's bug class—removal of a privilege/ownership check that let an unauthorized caller trigger transfers on behalf of another entity—the closest reachable Nebula analog is a missing binding check in the relay control-message handler that lets any authenticated peer register itself as a relay for an arbitrary third-party VPN address, poisoning the receiving node's relay/hostmap state and enabling traffic redirection.

### Title
Missing binding check in `handleCreateRelayRequest` lets any authenticated peer register itself as a relay for an arbitrary victim address, poisoning relay/hostmap state - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest()` processes `NebulaControl_CreateRelayRequest` messages received over an already-authenticated tunnel (`h *HostInfo`). It validates that the claimed `RelayFromAddr` is not the local node's own address, but it never validates that `RelayFromAddr` corresponds to the actual identity of the sending peer `h`. Any peer with a valid CA-signed cert (an ordinary, non-relay node) can therefore claim to be relaying on behalf of an arbitrary third-party VPN address and have the receiving node install `Established` relay state for it.

### Finding Description
When the target address of the request is the local node, the handler does: [1](#0-0) 

`from` comes directly from the attacker-controlled `m.RelayFromAddr` field of the control message and is only checked against the local node's own addresses (self-relay protection), never against `h.vpnAddrs` (the actual, cert-verified identity of the connected peer): [2](#0-1) 

If no existing relay state exists for that `from` address, `AddRelay(..., TerminalType, Established)` is called immediately, without any handshake with, or verification of, the claimed `from` identity: [3](#0-2) 

This mirrors the reported Solidity bug: a privilege/ownership check binding the actor to the resource it claims to act on behalf of was never enforced, so any already-authenticated principal can act as if it were a different, unrelated principal.

### Impact Explanation
Because the newly created relay entry is `Established` immediately and keyed by the attacker-supplied `from` address in the *target's own hostmap* (`h.relayState`, where `h` is the attacker's `HostInfo`), any subsequent outbound traffic that the target node needs to send to that `from` VPN address (when it has no better route) will be routed through the attacker's tunnel via `SendVia`/`QueryVpnAddrsRelayFor`: [4](#0-3) [5](#0-4) 

This lets a non-relay, fully-authenticated-but-unauthorized peer poison the target's relay routing state and position itself to intercept/redirect traffic addressed to an arbitrary victim VPN address it never actually established a tunnel with — a concrete remote state-poisoning / traffic-redirection primitive reachable purely by holding a valid CA-signed cert (no elevated `am_relay` privilege is required for this branch).

### Likelihood Explanation
Any node that can complete a normal Nebula handshake (i.e., holds a cert signed by the network's CA) can send this control message over its own established tunnel; no lighthouse, relay, or admin privilege is required to reach this code path since the check only guards against claiming to be the target itself, not against claiming to be a third party. This is a low-complexity, remotely triggerable path reachable by any authenticated peer.

### Recommendation
Bind `RelayFromAddr` to the sender's verified identity: reject (or specially validate) `CreateRelayRequest` messages where `from` does not correspond to `h.vpnAddrs` unless `h` is a legitimate, already-trusted relay for that address (e.g., only accept requests where the peer is either the claimed `from` address itself, or is a node explicitly configured/authorized as a relay for `from`, mirroring the existing `rm.GetAmRelay()` gate used in the forwarding branch).

### Proof of Concept
1. Attacker node `A` completes a normal Nebula handshake with victim/target node `T` (any valid CA-signed cert suffices; `A` does not need `am_relay` set).
2. `A` sends `NebulaControl_CreateRelayRequest` to `T` over its authenticated tunnel with `RelayFromAddr = V` (an arbitrary victim VPN address that `A` has no real tunnel to) and `RelayToAddr = T`'s own address.
3. `T`'s `handleCreateRelayRequest` sees `target == me`, finds no existing relay for `V`, and calls `AddRelay(..., V, TerminalType, Established)` on `A`'s `HostInfo`, per [3](#0-2) .
4. `T` now believes `A` is an established relay path to `V`. Any future traffic `T` needs to send to `V` (absent a direct/better route) is sent via `A` through `SendVia`, giving `A` the ability to observe/drop/redirect that traffic.

### Citations

**File:** relay_manager.go (L426-444)
```go
func (rm *relayManager) handleCreateRelayRequest(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	//nil-checks for protoAddrToNetAddr handled by caller
	from := protoAddrToNetAddr(m.RelayFromAddr)
	target := protoAddrToNetAddr(m.RelayToAddr)

	logMsg := rm.l.With(
		"relayFrom", from,
		"relayTo", target,
		"initiatorRelayIndex", m.InitiatorRelayIndex,
		"vpnAddrs", h.vpnAddrs,
	)

	logMsg.Info("handleCreateRelayRequest")
	// Is the source of the relay me? This should never happen, but did happen due to
	// an issue migrating relays over to newly re-handshaked host info objects.
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}
```

**File:** relay_manager.go (L446-487)
```go
	// Is the target of the relay me?
	if f.myVpnAddrsTable.Contains(target) {
		existingRelay, ok := h.relayState.QueryRelayForByIp(from)
		if ok {
			switch existingRelay.State {
			case Requested:
				ok = h.relayState.CompleteRelayByIP(from, m.InitiatorRelayIndex)
				if !ok {
					logMsg.Error("Relay State not found")
					return
				}
			case Established:
				if existingRelay.RemoteIndex != m.InitiatorRelayIndex {
					// We got a brand new Relay request, because its index is different than what we saw before.
					// This should never happen. The peer should never change an index, once created.
					logMsg.Error("Existing relay mismatch with CreateRelayRequest",
						"existingRemoteIndex", existingRelay.RemoteIndex)
					return
				}
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
			case PeerRequested:
				// I should never be in this state, because I am terminal, not forwarding.
				logMsg.Error("Unexpected Relay State found",
					"existingRemoteIndex", existingRelay.RemoteIndex,
					"state", existingRelay.State)
			}
		} else {
			_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
			if err != nil {
				logMsg.Error("Failed to add relay", "error", err)
				return
			}
		}
```

**File:** inside.go (L414-428)
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
```

**File:** hostmap.go (L583-613)
```go
func (hm *HostMap) QueryVpnAddrsRelayFor(targetIps []netip.Addr, relayHostIp netip.Addr) (*HostInfo, *Relay, error) {
	hm.RLock()
	defer hm.RUnlock()

	// This runs per relayed packet, so check the primary with a single map probe and only consult
	// moreHosts when the primary can't relay for us.
	h, ok := hm.Hosts[relayHostIp]
	if !ok {
		return nil, nil, errors.New("unable to find host")
	}

	for _, targetIp := range targetIps {
		r, ok := h.relayState.QueryRelayForByIp(targetIp)
		if ok && r.State == Established {
			return h, r, nil
		}
	}

	if list, ok := hm.moreHosts[relayHostIp]; ok {
		// list[0] is the primary we already checked
		for _, h := range list[1:] {
			for _, targetIp := range targetIps {
				r, ok := h.relayState.QueryRelayForByIp(targetIp)
				if ok && r.State == Established {
					return h, r, nil
				}
			}
		}
	}

	return nil, nil, errors.New("unable to find host with relay")
```
