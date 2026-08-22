### Title
Relay control messages let an authenticated peer register itself as the relay terminal/forwarder for an arbitrary spoofed `RelayFromAddr`, poisoning relay routing state - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest()` extracts `from` (`RelayFromAddr`) and `target` (`RelayToAddr`) directly from the attacker-controlled `NebulaControl` payload and uses `from` to create relay state — without ever checking that `from` equals the actual, cert-verified VPN address of the sending peer, `h.vpnAddrs[0]`. This is the same bug class as the Gondi finding: a value that is validated/expected to match an authenticated identity (`executionData.tokenId` vs. `loan.nftCollateralTokenId`) is substituted with an unrelated, attacker-supplied value (`RelayFromAddr` vs. `h.vpnAddrs[0]`) that drives the security-relevant state change.

### Finding Description
`HandleControlMsg` only checks that `RelayFromAddr`/`RelayToAddr` are non-nil before dispatching to the handlers: [1](#0-0) 

`handleCreateRelayRequest` then reads `from`/`target` straight from the message and uses `from` to build relay state, but the code path that matters — the “target is me” branch — never confirms that `from` matches `h.vpnAddrs[0]`, the address that was actually authenticated for `h` during the handshake: [2](#0-1) 

`h` is the already-verified `*HostInfo` for the peer that physically sent this control message (its identity was bound during handshake/cert verification, see `handshake_manager.go`’s `beginHandshake`/`continueHandshake` and `validatePeerCert`), yet `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` associates `h`’s hostinfo with `from` — a value taken purely from the message body, with no relation enforced to `h`’s real address: [3](#0-2) 

This mirrors the Gondi root cause precisely: the value that is validated (implicitly, via the authenticated tunnel identity `h.vpnAddrs[0]`) is not the value that is actually used to establish the relay association (`from`). Any authenticated peer can claim to be relaying on behalf of an arbitrary victim IP.

The forwarding ("I'm a relay") branch has the same issue — `from` is used to register `ForwardingType` relay state on the real target's hostinfo, and is echoed back to that target as the peer's identity, again without validation against `h`'s actual address: [4](#0-3) 

That poisoned relay entry is later trusted by the data-plane relay/forwarding lookup, which resolves purely by index/address without re-verifying the original claim: [5](#0-4) [6](#0-5) 

### Impact Explanation
An authenticated (but otherwise ordinary, unprivileged) peer can cause a relay-capable node to register itself (or induce a third node) as the Terminal or Forwarding relay endpoint for a victim's VPN address that it does not own. Depending on topology, this can redirect or intercept traffic that other nodes intend to route to the victim through this relay, i.e., remote relay-state poisoning that can lead to traffic misdirection/interception for a third party — without ever presenting a certificate for that victim address. This matches the "remote state poisoning" / traffic redirection impact class called out as acceptable.

### Likelihood Explanation
Exploitation requires only an established, otherwise-ordinary Nebula tunnel (any peer trusted by the CA, no special privilege), and sending a single crafted `NebulaControl` `CreateRelayRequest` message with a `RelayFromAddr` set to an arbitrary victim address. No additional capability is required beyond what any legitimate but low-trust peer already has, and the relevant validation gap is a straightforward missing equality check, making this readily reachable.

### Recommendation
In `handleCreateRelayRequest` (and defensively in `handleCreateRelayResponse`), reject the message unless `from` equals `h.vpnAddrs[0]` (or, for v2 multi-address certs, is contained in `h`'s certified VPN networks). Do not trust the wire-supplied `RelayFromAddr`/`OldRelayFromAddr` as the peer's identity for state-mutating operations; derive the "from" identity strictly from the authenticated `HostInfo`/certificate associated with the tunnel the control message arrived on.

### Proof of Concept
1. Peer `A` (address `10.0.0.5`, validly certified) establishes a normal Nebula tunnel to relay-capable node `M`.
2. `A` sends `M` a `Control`/`CreateRelayRequest` message with `RelayFromAddr = 10.0.0.99` (victim `V`, not `A`'s own address) and `RelayToAddr = M`'s address.
3. In `relayManager.handleCreateRelayRequest`, since `target` matches `f.myVpnAddrsTable`, `M` calls `AddRelay(rm.l, h, f.hostMap, from=10.0.0.99, ..., TerminalType, Established)` — associating `A`'s already-authenticated `HostInfo` with victim `V`'s address as an established terminal relay, with no check that `10.0.0.99` is actually `A`.
4. Any subsequent lookup via `HostMap.QueryVpnAddrsRelayFor([10.0.0.99], A)` on `M` will now succeed and treat `A` as the relay endpoint for `V`, letting `A` receive/intercept traffic intended for `V` through `M`.

### Citations

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

**File:** relay_manager.go (L426-487)
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

**File:** relay_manager.go (L525-552)
```go
	} else {
		// the target is not me. Create a relay to the target, from me.
		if !rm.GetAmRelay() {
			return
		}
		peer := rm.hostmap.QueryVpnAddr(target)
		if peer == nil {
			// Try to establish a connection to this host. If we get a future relay request,
			// we'll be ready!
			f.Handshake(target)
			return
		}
		if !peer.GetRemote().IsValid() {
			// Only create relays to peers for whom I have a direct connection
			return
		}
		var index uint32
		var err error
		targetRelay, ok := peer.relayState.QueryRelayForByIp(from)
		if ok {
			index = targetRelay.LocalIndex
		} else {
			// Allocate an index in the hostMap for this relay peer
			index, err = AddRelay(rm.l, peer, f.hostMap, from, nil, ForwardingType, Requested)
			if err != nil {
				return
			}
		}
```

**File:** outside.go (L176-234)
```go
func (f *Interface) handleOutsideRelayPacket(hostinfo *HostInfo, via ViaSender, out []byte, packet []byte, h *header.H, fwPacket *firewall.Packet, lhf *LightHouseHandler, nb []byte, q int, localCache firewall.ConntrackCache) {
	// Successfully validated the thing. Get rid of the Relay header and the AEAD tag
	signedPayload := packet[header.Len : len(packet)-hostinfo.ConnectionState.dKey.Overhead()]
	// Pull the Roaming parts up here, and return in all call paths.
	f.handleHostRoaming(hostinfo, via)
	// Track usage of both the HostInfo and the Relay for the received & authenticated packet
	f.connectionManager.In(hostinfo)
	f.connectionManager.RelayUsed(h.RemoteIndex)

	relay, ok := hostinfo.relayState.QueryRelayForByIdx(h.RemoteIndex)
	if !ok {
		// The only way this happens is if hostmap has an index to the correct HostInfo, but the HostInfo is missing
		// its internal mapping. This should never happen.
		hostinfo.logger(f.l).Error("HostInfo missing remote relay index",
			"relayRemoteIndex", h.RemoteIndex,
		)
		return
	}

	switch relay.Type {
	case TerminalType:
		// If I am the target of this relay, process the unwrapped packet
		// From this recursive point, all these variables are 'burned'. We shouldn't rely on them again.
		via = ViaSender{
			UdpAddr:   via.UdpAddr,
			relayHI:   hostinfo,
			relay:     relay,
			IsRelayed: true,
		}
		f.readOutsidePackets(via, out[:0], signedPayload, h, fwPacket, lhf, nb, q, localCache)
	case ForwardingType:
		// Find the target HostInfo relay object
		targetHI, targetRelay, err := f.hostMap.QueryVpnAddrsRelayFor(hostinfo.vpnAddrs, relay.PeerAddr)
		if err != nil {
			hostinfo.logger(f.l).Info("Failed to find target host info by ip",
				"relayTo", relay.PeerAddr,
				"relayFrom", hostinfo.vpnAddrs[0],
				"error", err,
			)
			return
		}

		// If that relay is Established, forward the payload through it
		if targetRelay.State == Established {
			switch targetRelay.Type {
			case ForwardingType:
				// Forward this packet through the relay tunnel, rebuilding it in place.
				// Encode overwrites the old outer header, and the new AEAD tag lands where the old one was
				fwdBuf := packet[:0:len(packet)] // Cap to len(packet) to protect memory from a larger parent buffer
				f.SendVia(targetHI, targetRelay, signedPayload, nb, fwdBuf, true)
			case TerminalType:
				hostinfo.logger(f.l).Error("Unexpected Relay Type of Terminal")
				return
			default:
				if f.l.Enabled(context.Background(), slog.LevelDebug) {
					hostinfo.logger(f.l).Debug("Unexpected targetRelay Type", "from", via, "relayType", targetRelay.Type)
				}
				return
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
