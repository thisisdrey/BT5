### Title
Relay `CreateRelayRequest`/`CreateRelayResponse` trust the attacker-supplied `RelayFromAddr`/`RelayToAddr` fields without binding them to the authenticated sender's certificate identity - (File: `relay_manager.go`)

### Summary
In Nebula's relay protocol, a node acting as a relay ("me") or as a relay target ("them") accepts the `RelayFromAddr` and `RelayToAddr` fields carried in a `NebulaControl` message at face value, without verifying that the claimed address matches the actual, cert-authenticated identity (`h.vpnAddrs[0]`) of the peer that sent the message over the already-established, encrypted tunnel `h`. This mirrors the Sherlock finding: a shared/blindly-trusted authorization parameter (there, the vault-authority PDA; here, the relay identity binding) is accepted without checking that the party performing the action is actually who they claim to be.

### Finding Description
`relayManager.handleCreateRelayRequest` receives a control message over an authenticated tunnel identified by `h *HostInfo` (whose real identity is `h.vpnAddrs[0]`, bound during the Noise handshake and certificate verification in `handshake_manager.go` `validatePeerCert`/`certVerifier`). However, the function derives `from`/`target` purely from attacker-controlled protobuf fields: [1](#0-0) 

The only identity check performed is `f.myVpnAddrsTable.Contains(from)` (rejecting only the degenerate case where the claimed "from" is the local node itself) - there is no check that `from == h.vpnAddrs[0]`, i.e., no verification that the sender of the control message is actually the party it claims to be the relay-from for.

When `target` (also attacker-supplied) matches the local node's own address, the code installs a `TerminalType` relay binding `from` (spoofable) to `h` (the real, authenticated tunnel to the attacker): [2](#0-1) 

`AddRelay` stores this binding keyed by the attacker-chosen `vpnIp` (`from`) pointing at `relayHostInfo` (`h`, the attacker's own tunnel): [3](#0-2) 

That binding is later consulted by any third node forwarding relayed traffic. `HostMap.QueryVpnAddrsRelayFor` looks up a relay purely by `relayHostIp` and the claimed peer address, with no re-verification against certificate identity at forward time: [4](#0-3) 

And `handleOutsideRelayPacket`'s `ForwardingType` branch simply forwards any packet whose relay/idx mapping resolves, again trusting the previously-installed (spoofable) `PeerAddr` binding: [5](#0-4) 

The `handleCreateRelayResponse` path exhibits the same pattern - it trusts `m.RelayFromAddr`/`m.RelayToAddr` to look up and re-key relay state (`rm.hostmap.QueryVpnAddr(relay.PeerAddr)`, `peerHostInfo.relayState.QueryRelayForByIp(relayTo)`) without cross-checking against the authenticated identity of the peer that actually sent the response: [6](#0-5) 

By contrast, the direct (non-relayed) handshake path in `handshake_manager.go` does bind the certificate's VPN networks to the actual responding party (`correctHostResponded` check), showing that Nebula elsewhere considers this kind of identity binding a required security control - but that control is missing in the relay-control-message path: [7](#0-6) 

### Impact Explanation
An attacker holding a valid CA-signed certificate for their own identity (e.g. `attacker@10.128.0.99`) can, over their own legitimately-authenticated tunnel to a relay node `N`, send a `CreateRelayRequest` claiming `RelayFromAddr = victim's real VPN address`. `N` will install a `TerminalType`/`ForwardingType` relay entry that maps the victim's address to the attacker's own tunnel (`h`). Any subsequent traffic that other peers route through `N` intending to reach the victim (`relay.PeerAddr == victim`) will instead be forwarded to the attacker's tunnel, redirecting/hijacking traffic meant for the victim - remote relay-state poisoning enabling traffic redirection and potential impersonation of the victim within the mesh, without needing the victim's certificate or any compromise of the victim's tunnel.

### Likelihood Explanation
This requires only a normal, validly-certified Nebula node (no attacker-controlled lighthouse, no valid-certificate-holder-of-victim, and no host-access to the victim) that has `relay.use_relays`/being a relay peer, and the ability to reach a node that is a relay for third parties or is `relay.am_relay`. Given how simple the field manipulation is (attacker fully controls their own outbound `NebulaControl` message fields), and that no additional cryptographic material or race condition is needed, likelihood is Medium-to-High wherever relays are used.

### Recommendation
When processing `CreateRelayRequest`/`CreateRelayResponse`, validate that the claimed `RelayFromAddr` (and, symmetrically, `RelayToAddr` where applicable) matches `h.vpnAddrs` - the certificate-verified identity bound to the authenticated `HostInfo` that actually delivered the control message - and reject/log any mismatch instead of trusting attacker-supplied address fields to key relay state.

### Proof of Concept
Not executed; this is a static-analysis finding based on code inspection of `relay_manager.go`, `hostmap.go`, and `outside.go`. A concrete PoC would require standing up three Nebula nodes (victim, relay, attacker) with a shared CA, having the attacker send a crafted `CreateRelayRequest` with `RelayFromAddr` set to the victim's VPN address, and observing that the relay node subsequently forwards victim-addressed relayed traffic to the attacker's tunnel - this was not run as part of this review.

### Citations

**File:** relay_manager.go (L227-268)
```go
// AddRelay finds an available relay index on the hostmap, and associates the relay info with it.
// relayHostInfo is the Nebula peer which can be used as a relay to access the target vpnIp.
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

**File:** relay_manager.go (L344-424)
```go
func (rm *relayManager) handleCreateRelayResponse(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	//nil-checks for protoAddrToNetAddr handled by caller
	relayFrom := protoAddrToNetAddr(m.RelayFromAddr)
	relayTo := protoAddrToNetAddr(m.RelayToAddr)
	rm.l.Info("handleCreateRelayResponse",
		"relayFrom", relayFrom,
		"relayTo", relayTo,
		"initiatorRelayIndex", m.InitiatorRelayIndex,
		"responderRelayIndex", m.ResponderRelayIndex,
		"vpnAddrs", h.vpnAddrs,
	)

	relay, err := rm.EstablishRelay(h, m)
	if err != nil {
		rm.l.Error("Failed to update relay for relayTo", "error", err)
		return
	}
	// Do I need to complete the relays now?
	if relay.Type == TerminalType {
		return
	}
	// I'm the middle man. Let the initiator know that the I've established the relay they requested.
	peerHostInfo := rm.hostmap.QueryVpnAddr(relay.PeerAddr)
	if peerHostInfo == nil {
		rm.l.Error("Can't find a HostInfo for peer", "relayTo", relay.PeerAddr)
		return
	}
	peerRelay, ok := peerHostInfo.relayState.QueryRelayForByIp(relayTo)
	if !ok {
		rm.l.Error("peerRelay does not have Relay state for relayTo", "relayTo", peerHostInfo.vpnAddrs[0])
		return
	}
	switch peerRelay.State {
	case Requested:
		// I initiated the request to this peer, but haven't heard back from the peer yet. I must wait for this peer
		// to respond to complete the connection.
	case PeerRequested, Disestablished, Established:
		peerHostInfo.relayState.UpdateRelayForByIpState(relayTo, Established)
		resp := NebulaControl{
			Type:                NebulaControl_CreateRelayResponse,
			ResponderRelayIndex: peerRelay.LocalIndex,
			InitiatorRelayIndex: peerRelay.RemoteIndex,
		}

		peer := peerHostInfo.vpnAddrs[0]
		if v == cert.Version1 {
			if !peer.Is4() {
				rm.l.Error("Refusing to CreateRelayResponse for a v1 relay with an ipv6 address",
					"relayFrom", peer,
					"relayTo", relayTo,
					"initiatorRelayIndex", resp.InitiatorRelayIndex,
					"responderRelayIndex", resp.ResponderRelayIndex,
					"vpnAddrs", peerHostInfo.vpnAddrs,
				)
				return
			}

			b := peer.As4()
			resp.OldRelayFromAddr = binary.BigEndian.Uint32(b[:])
			b = relayTo.As4()
			resp.OldRelayToAddr = binary.BigEndian.Uint32(b[:])
		} else {
			resp.RelayFromAddr = netAddrToProtoAddr(peer)
			resp.RelayToAddr = m.RelayToAddr
		}

		msg, err := resp.Marshal()
		if err != nil {
			rm.l.Error("relayManager Failed to marshal Control CreateRelayResponse message to create relay", "error", err)
			return
		}
		f.SendMessageToHostInfo(header.Control, 0, peerHostInfo, msg, make([]byte, 12), make([]byte, mtu))
		rm.l.Info("send CreateRelayResponse",
			"relayFrom", peer,
			"relayTo", relayTo,
			"initiatorRelayIndex", resp.InitiatorRelayIndex,
			"responderRelayIndex", resp.ResponderRelayIndex,
			"vpnAddrs", peerHostInfo.vpnAddrs,
		)
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

**File:** relay_manager.go (L446-493)
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

		relay, ok := h.relayState.QueryRelayForByIp(from)
		if !ok {
			logMsg.Error("Relay State not found", "from", from)
			return
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

**File:** outside.go (L206-234)
```go
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

**File:** handshake_manager.go (L891-921)
```go
	// Verify correct host responded (initiator check)
	vpnAddrs := make([]netip.Addr, len(vpnNetworks))
	correctHostResponded := false
	anyVpnAddrsInCommon := false
	for i, network := range vpnNetworks {
		// inside.go drops self-routed packets at the firewall stage, but we'd
		// rather not let a self-handshake complete in the first place: it
		// wastes a hostmap slot, suppresses no log, and obscures routing
		// misconfig. Explicit refusal here mirrors the responder-side check
		// in validatePeerCert.
		if f.myVpnAddrsTable.Contains(network.Addr()) {
			f.l.Error("Refusing to handshake with myself",
				"vpnNetworks", vpnNetworks,
				"from", via,
				"certName", remoteCert.Certificate.Name(),
				"certVersion", remoteCert.Certificate.Version(),
				"fingerprint", remoteCert.Fingerprint,
				"issuer", remoteCert.Certificate.Issuer(),
				"handshake", m{"stage": uint64(machine.MessageIndex()), "style": header.SubTypeName(header.Handshake, machine.Subtype())},
			)
			hm.DeleteHostInfo(hostinfo)
			return
		}
		vpnAddrs[i] = network.Addr()
		if hostinfo.vpnAddrs[0] == network.Addr() {
			correctHostResponded = true
		}
		if f.myVpnNetworksTable.Contains(network.Addr()) {
			anyVpnAddrsInCommon = true
		}
	}
```
