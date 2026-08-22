### Title
Relay control messages allow an authenticated peer to poison peer relay state via unverified `RelayFromAddr` — (`File: relay_manager.go`)

### Summary
Nebula's relay subsystem lets a peer act as a forwarding hop (`am_relay`) for traffic between two other nodes. Establishment of a relay is negotiated with `NebulaControl_CreateRelayRequest`/`CreateRelayResponse` messages carried inside an already-authenticated Nebula tunnel. The `from`/`to` VPN addresses in these control messages are attacker-supplied protobuf fields (`RelayFromAddr`/`RelayToAddr`), and `relayManager.handleCreateRelayRequest` trusts the `RelayFromAddr` value as the identity on whose behalf the sending peer is forwarding — without ever checking that value against the sending peer's certificate-verified `vpnAddrs`. This mirrors the STON.fi bug class: an address field taken from an attacker-controlled payload is used as an implicit "on behalf of" identity for a subsequent privileged state-mutating operation, instead of using the caller's own verified identity.

### Finding Description
When a Control message of type `CreateRelayRequest` arrives, `relayManager.HandleControlMsg` unmarshals it and dispatches to `handleCreateRelayRequest`: [1](#0-0) 

Inside `handleCreateRelayRequest`, `from` and `target` are taken directly from the message body (`m.RelayFromAddr`/`m.RelayToAddr`), which is fully attacker-controlled since it is simply parsed protobuf data, not anything cryptographically bound to the sender's certificate: [2](#0-1) 

If `target` (i.e. `RelayToAddr`) is my own VPN address, the code treats `h` (the `HostInfo` of the authenticated peer that physically sent this Control message) as a valid forwarding relay for the VPN address `from`, and installs that mapping into persistent relay state via `AddRelay`: [3](#0-2) 

Nowhere in this path is `from` checked against `h`'s own certificate-verified `vpnAddrs` (as is done elsewhere for self-connection checks, e.g. `f.myVpnAddrsTable.Contains(from)` at line 441, which only rejects the case where `from == me`). Any legitimately-authenticated peer `h` can therefore claim `RelayFromAddr` = an arbitrary third-party VPN address (including a real, unrelated victim node's address) that has no relationship to `h`'s own identity, and the receiver will record `h` as a `TerminalType`, `Established` relay path for that address: [4](#0-3) 

This state is later consulted for relayed traffic (`hostinfo.relayState.QueryRelayForByIdx`, `QueryVpnAddrsRelayFor`) when the local node has no direct remote for a target and falls back to relaying: [5](#0-4) [6](#0-5) 

The same unverified-address pattern is present symmetrically in `StartRelays` where an authenticated relay's claims are trusted without cross-checking against the certificate of the party being relayed to, and in `handleCreateRelayResponse`, which likewise processes `RelayFromAddr`/`RelayToAddr` without independent verification: [7](#0-6) 

### Impact Explanation
An authenticated but malicious peer can poison a victim's relay routing state (`HostMap.Relays`, `HostInfo.relayState`) by claiming to be a relay path for an arbitrary VPN address it does not own and has no certificate relationship to. This is a remote state-poisoning primitive: it can redirect where the victim attempts to route relayed traffic/handshake attempts for other nodes, enabling denial-of-service against tunnel establishment to the spoofed address, or hijacking of the relay path selection so the attacker sits in the forwarding path for handshake/control traffic intended for a different node — the exact "trust an address field instead of the verified caller identity" data-validation flaw the STON.fi report describes, applied to Nebula's relay negotiation state machine.

### Likelihood Explanation
Reachable by any peer that has completed a normal mutually-authenticated handshake (i.e., holds a valid, CA-signed certificate for their own identity) but does not require any special privilege beyond being a normal member of the mesh; no additional certificate forgery is needed since the attack only requires spoofing the `RelayFromAddr`/`RelayToAddr` fields inside an otherwise legitimately-encrypted Control message. This is a low-difficulty, low-cost attack once a peer is part of the mesh.

### Recommendation
Validate that `RelayFromAddr` in a `CreateRelayRequest`/`CreateRelayResponse` corresponds either to the sending peer's own certificate-verified `vpnAddrs` (when the sender is the relay initiator) or is otherwise cryptographically attributable, before installing relay state that associates that address with the sending `HostInfo`. Short term, reject requests where `from` is not contained in `h.vpnAddrs` unless the message is demonstrably being forwarded on behalf of a peer that itself has a verified, established relay chain to that address. Long term, define and enforce a state-transition specification for relay establishment so that relay identity claims are always derived from certificate-verified data rather than untrusted message fields.

### Proof of Concept
1. Peer `M` completes a normal handshake with target node `C` (both hold valid certs signed by the shared CA) and is configured with `relay.use_relays: true`.
2. `M` crafts a `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to `V`'s VPN address (a real, unrelated node that `M` has no relationship with) and `RelayToAddr` set to `C`'s own VPN address, then sends it to `C` over the already-established encrypted tunnel (`header.Control`), matching the message the legitimate `StartRelays`/`handleCreateRelayRequest` path expects: [2](#0-1) 
3. `C`'s `outside.go:handleOutsideRelayPacket`/`Interface` dispatches the Control message to `relayManager.HandleControlMsg` → `handleCreateRelayRequest`, which finds `target == C` (itself), and since no relay exists for `from = V`, calls `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`, installing `M` as an established relay path for `V` in `C`'s hostmap without ever checking that `M`'s own certificate has any relation to `V`: [3](#0-2) 
4. Subsequent attempts by `C` to relay traffic toward `V` (when no direct tunnel exists) will consult this poisoned relay state and attempt to route via `M`, as seen in the relay lookup/forwarding logic used both for handshakes (`StartRelays`) and data (`sendNoMetrics`/`handleOutsideRelayPacket`): [5](#0-4)

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

**File:** relay_manager.go (L426-447)
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
```

**File:** relay_manager.go (L481-493)
```go
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
