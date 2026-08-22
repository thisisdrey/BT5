### Title
Unauthenticated `RelayFromAddr` in `CreateRelayRequest` allows relay-state poisoning for arbitrary vpnAddrs - ([File: relay_manager.go])

### Summary
`relayManager.HandleControlMsg` / `handleCreateRelayRequest` never checks that the `RelayFromAddr` claimed in a `CreateRelayRequest` matches the certificate-verified `vpnAddrs` of the HostInfo that actually sent the message. A peer that has completed any legitimate (unprivileged) handshake can therefore claim to be relaying for a victim vpnAddr it does not own, causing the receiving node to insert an `Established` `Relay` entry keyed by that victim address on the attacker's own `HostInfo`.

### Finding Description
The packet path is `readOutsidePackets` (`header.Control`) → `relayManager.HandleControlMsg` → `handleCreateRelayRequest`: [1](#0-0) 

`HandleControlMsg` only validates that `RelayFromAddr`/`RelayToAddr` are non-nil; it performs no check against the sender's certified identity: [2](#0-1) 

In `handleCreateRelayRequest`, `from` is decoded straight from the attacker-controlled message field and is only checked against the *local* node's own addresses (`f.myVpnAddrsTable.Contains(from)`), never against `h.vpnAddrs` (the actual, cert-verified identity of the peer `h` that sent this control message): [3](#0-2) 

When the relay target is the local node (`target == me`), the code immediately inserts a `TerminalType` relay in `Established` state keyed by the attacker-supplied `from`, on the attacker's own `HostInfo` object, with zero additional verification and zero round-trip confirmation: [4](#0-3) 

`AddRelay` itself performs no ownership/cert check either — it just wires the `Relay{PeerAddr: vpnIp}` into `hm.Relays` and the caller-supplied `HostInfo`'s `relayState`: [5](#0-4) 

This state is exactly what `HostMap.QueryVpnAddrsRelayFor` later trusts to route/relay traffic once some other code path selects the attacker's real vpnAddr as a relay for that (unrelated) target: [6](#0-5) 

The invariant described in the question — "relay path establishment must validate that the requesting peer's certificate actually authorizes relaying to/from the named vpnAddr" — is not enforced anywhere in this call chain.

### Impact Explanation
An attacker with any valid, low-privilege cert and a completed handshake to some peer P can plant a bogus `Established` `TerminalType` relay entry on P for any vpnAddr of their choosing, without owning or being authorized for that address. If P (or a third node using P as a relay) subsequently selects the attacker's real address as a candidate relay for that victim address — a state reachable in deployments where nodes share a small set of relay-capable peers — traffic P believes is being forwarded to the victim through the attacker is instead delivered directly into the attacker's own authenticated tunnel, since the relay entry is `Established` under the attacker's own `HostInfo`/session keys. This is remote relay/session-state poisoning and a potential traffic-steering/hijack primitive, matching the "remote state poisoning" / traffic-forgery impact category.

### Likelihood Explanation
The attack requires nothing beyond a normal unprivileged handshake and the ability to send a single `header.Control` `CreateRelayRequest` packet naming an arbitrary `RelayToAddr = me` and `RelayFromAddr = <victim>` — no elevated cert attributes are needed, and the code path unconditionally accepts it. Full exploitation (redirecting real victim traffic) additionally requires the attacker's own address to later be treated as a viable relay candidate for the victim by some other node, which is deployment-dependent, but the underlying state-poisoning primitive itself is reliably and repeatably triggerable by any attacker with a session.

### Recommendation
In `handleCreateRelayRequest` (and `handleCreateRelayResponse`/`EstablishRelay`), verify that the peer sending the control message is actually authorized for the claimed `RelayFromAddr`: for a request where the local node is the terminal target, require `from` to be contained in `h.vpnAddrs` (the cert-verified identity of the sender `h`), and reject/log-drop otherwise. Similarly ensure `CompleteRelayByIdx`/`EstablishRelay` cross-check the claimed address fields against the pre-existing `Relay.PeerAddr` rather than trusting them unconditionally.

### Proof of Concept
Unit test in `relay_manager_test.go` style (similar to existing `TestRelayManager_HandleControlMsg_NilRelayAddrs`):
1. Build a `relayManager` with a real `hostmap`, and an `Interface` `f` whose `myVpnAddrsTable` contains only `me`'s own address.
2. Construct a `HostInfo h` representing an attacker's legitimately-handshaked session with `vpnAddrs = [attackerAddr]`.
3. Craft a `NebulaControl{Type: CreateRelayRequest, RelayFromAddr: victimAddr, RelayToAddr: meAddr}` and call `rm.HandleControlMsg(h, msg, f)`.
4. Assert (expected to currently fail, proving the bug): `h.relayState.QueryRelayForByIp(victimAddr)` should NOT return an `Established`/

### Citations

**File:** outside.go (L168-169)
```go
	case header.Control:
		f.relayManager.HandleControlMsg(hostinfo, out, f)
```

**File:** relay_manager.go (L229-268)
```go
func AddRelay(l *slog.Logger, relayHostInfo *HostInfo, hm *HostMap, vpnIp netip.Addr, remoteIdx *uint32, relayType int, state int) (uint32, error) {
	hm.Lock()
	defer hm.Unlock()
	for range 32 {
		index, err := generateIndex(l)
		if err != nil {
			return 0, err
		}

		_, inRelays := hm.Relays[index]
		if !inRelays {
			// Avoid standing up a relay that can't be used since only the primary hostinfo
			// will be pointed to by the relay logic
			//TODO: if there was an existing primary and it had relay state, should we merge?
			if !hm.unlockedMakePrimary(relayHostInfo) {
				// The tunnel was torn down after the caller grabbed relayHostInfo. A relay standing
				// on an unlinked hostinfo would never carry traffic, and its Relays entry could
				// never be reclaimed since the delete-time cleanup has already run.
				return 0, errors.New("relay hostinfo is no longer in the hostmap")
			}

			hm.Relays[index] = relayHostInfo
			newRelay := Relay{
				Type:       relayType,
				State:      state,
				LocalIndex: index,
				PeerAddr:   vpnIp,
			}

			if remoteIdx != nil {
				newRelay.RemoteIndex = *remoteIdx
			}
			relayHostInfo.relayState.InsertRelay(vpnIp, index, &newRelay)

			return index, nil
		}
	}

	return 0, errors.New("failed to generate unique localIndexId")
}
```

**File:** relay_manager.go (L320-334)
```go
	// validate:
	switch msg.Type {
	case NebulaControl_CreateRelayRequest, NebulaControl_CreateRelayResponse:
		if msg.RelayFromAddr == nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("Control message received with nil RelayFromAddr", "type", msg.Type)
			}
			return
		} else if msg.RelayToAddr == nil {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("Control message received with nil RelayToAddr", "type", msg.Type)
			}
			return
		}
	}
```

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

**File:** hostmap.go (L583-614)
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
}
```
