### Title
Relay establishment trusts attacker-supplied `RelayFromAddr`/`RelayToAddr` fields instead of the cryptographically authenticated sender identity, enabling relay-state poisoning for arbitrary victim VPN addresses - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` and `handleCreateRelayResponse` build and forward relay state using the `RelayFromAddr`/`RelayToAddr` fields taken directly from the `NebulaControl` protobuf payload, without ever checking that the claimed "from" address actually matches the certificate-verified identity (`h.vpnAddrs`) of the peer that authenticated and sent the message. Just as the reported `MechMarketplace` bug allowed any party holding a valid signature to claim a delivery slot that was never bound to them, here any authenticated Nebula peer can claim to be relaying "on behalf of" a third-party VPN address it does not control, and the responder has no way to verify that binding.

### Finding Description
When peer `A` wants to route through relay `R` to reach `B`, `A` sends a `CreateRelayRequest` over its own authenticated tunnel to `R`. The message carries `RelayFromAddr` (supposed to be `A`'s own address) as a payload field: [1](#0-0) 

`handleCreateRelayRequest` decodes `from := protoAddrToNetAddr(m.RelayFromAddr)` and `target := protoAddrToNetAddr(m.RelayToAddr)` purely from the message body. The only self-referential checks performed are whether `from` or `target` equal *my own* address: [2](#0-1) 

There is no check anywhere in this function (or in `HandleControlMsg`'s pre-validation) that `from == h.vpnAddrs[0]`, where `h` is the `*HostInfo` whose already-authenticated Noise tunnel decrypted the Control message: [3](#0-2) 

Because Nebula's certificate/handshake machinery only authenticates *who sent the packet* (via `ConnectionState`/`h`), not *the semantic content of the relay claim inside the packet*, any peer with a valid tunnel to a relay `R` can substitute an arbitrary victim's VPN address into `RelayFromAddr` and have `R`:
- Build local relay state (`AddRelay`) keyed to the victim's address,
- Forward a fabricated `CreateRelayRequest` toward the intended target `B` claiming the victim wants a relay,
- Cause `B` to install a spurious, "Established" relay-state entry for the victim's address if `B` currently has no existing relay entry for it: [4](#0-3) 

The response path (`handleCreateRelayResponse`) exhibits the same trust-without-binding pattern, propagating attacker-influenced `RelayFromAddr`/`RelayToAddr`/index values onward to a "peer" looked up purely by VPN address, again with no verification that the address matches the identity that legitimately participated in the original exchange: [5](#0-4) 

This mirrors the `MechMarketplace` root cause precisely: a signed/authenticated request (there, a requester's signature; here, an authenticated Control message) is processed without deriving/verifying a binding between the message and the specific third party (`priority mech` there, a specific VPN peer here) that the protocol logic assumes it came from.

### Impact Explanation
An authenticated but malicious Nebula peer can poison another node's relay bookkeeping (`HostInfo.relayState`) with fabricated `Requested`/`Established` entries that claim to belong to a victim's VPN address, without that victim's participation or consent. This is a form of remote state poisoning: the relay peer and/or target end up believing a relay path exists for a VPN address that never authorized or initiated the relay. If that state is later consulted for real traffic routing decisions (e.g. `StartRelays`, `SendVia`), it can misdirect handshake/data traffic intended for the victim through an attacker-influenced path, and it consumes/occupies relay index slots that a legitimate future request from the real victim would then collide with or be rejected against (see the `Established`/`Disestablished` index-mismatch error paths in `handleCreateRelayRequest`).

### Likelihood Explanation
The attacker only needs an ordinary, valid Nebula certificate signed by the network's CA and a live tunnel to any node that participates in relaying (`relay.use_relays`/`relay.am_relay`) — no special privileges, no ability to forge certificates, and no man-in-the-middle position are required. Since `RelayFromAddr`/`RelayToAddr` are plain protobuf fields fully controlled by the sender and are never cross-checked against `h.vpnAddrs`, exploitation is a matter of sending a single crafted `CreateRelayRequest` Control message.

### Recommendation
When processing `CreateRelayRequest`/`CreateRelayResponse`, validate that `RelayFromAddr` (in the request-originator role) corresponds to the actual authenticated sender's certificate address (`h.vpnAddrs`), analogous to how the vpn address is already checked in other authenticated paths (e.g. `validatePeerCert`). Only when the sending `HostInfo` is acting purely as a forwarding relay (already-established `ForwardingType`/`Requested` relay state tied to that specific `HostInfo`) should the "on behalf of" address be trusted, and even then it should be tied to the specific relay session/index rather than freely supplied per-message.

### Proof of Concept
1. Stand up three nodes under one CA: `A` (attacker), `R` (a node with `relay.am_relay: true`), and `B` (target), plus a fourth `C` representing an uninvolved victim VPN address that never talks to `R` or `B`.
2. Establish a normal tunnel from `A` to `R`.
3. Have `A` send a `NebulaControl{Type: CreateRelayRequest, RelayFromAddr: <C's address>, RelayToAddr: <B's address>, InitiatorRelayIndex: <arbitrary>}` over its tunnel to `R` (bypassing the normal `StartRelays` helper, which would have used `f.myVpnAddrs[0]`, i.e. crafting the message directly as the reported bug's PoC crafted the signature directly).
4. Observe that `R.handleCreateRelayRequest` accepts the message (no check that `from == A`'s own address) and forwards a `CreateRelayRequest` toward `B` claiming address `C` wants a relay.
5. Observe on `B` that a new `Established` (or `Requested`) relay-state entry keyed to `C`'s VPN address is created in `B`'s hostmap even though `C` never sent anything, confirming the state-poisoning binding gap described above.

### Citations

**File:** relay_manager.go (L298-342)
```go
func (rm *relayManager) HandleControlMsg(h *HostInfo, d []byte, f *Interface) {
	msg := &NebulaControl{}
	err := msg.Unmarshal(d)
	if err != nil {
		h.logger(f.l).Error("Failed to unmarshal control message", "error", err)
		return
	}

	var v cert.Version
	if msg.OldRelayFromAddr > 0 || msg.OldRelayToAddr > 0 {
		v = cert.Version1

		b := [4]byte{}
		binary.BigEndian.PutUint32(b[:], msg.OldRelayFromAddr)
		msg.RelayFromAddr = netAddrToProtoAddr(netip.AddrFrom4(b))

		binary.BigEndian.PutUint32(b[:], msg.OldRelayToAddr)
		msg.RelayToAddr = netAddrToProtoAddr(netip.AddrFrom4(b))
	} else {
		v = cert.Version2
	}

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

	switch msg.Type {
	case NebulaControl_CreateRelayRequest:
		rm.handleCreateRelayRequest(v, h, f, msg)
	case NebulaControl_CreateRelayResponse:
		rm.handleCreateRelayResponse(v, h, f, msg)
	}
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

**File:** relay_manager.go (L426-493)
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

		relay, ok := h.relayState.QueryRelayForByIp(from)
		if !ok {
			logMsg.Error("Relay State not found", "from", from)
			return
		}
```
