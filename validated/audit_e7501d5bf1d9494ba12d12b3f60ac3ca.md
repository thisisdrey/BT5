### Title
Relay node trusts attacker-supplied `RelayFromAddr` without verifying it belongs to the requesting peer, enabling relayed-traffic hijack - ([File: relay_manager.go])

### Summary
`relayManager.handleCreateRelayRequest` accepts a `CreateRelayRequest` control message from an already-authenticated peer and instantiates relay state keyed on the `RelayFromAddr` field taken directly from the message body, without ever checking that this claimed VPN address is actually owned by the sending `HostInfo`. This mirrors the original report's bug class: an authenticated caller can act as though it owns an identity/resource (there `gtId`, here a VPN address) that a downstream function never verifies against the actual caller.

### Finding Description
When a relay node (`am_relay` host) receives a `NebulaControl_CreateRelayRequest` message over an already-established, decrypted tunnel from peer `h`, it unmarshals the attacker-controlled `RelayFromAddr`/`OldRelayFromAddr` field into `from`: [1](#0-0) 

If `from`'s stated target is the relay node itself, the code proceeds to register or complete relay state keyed on `from` and links it to `h`'s `HostInfo`: [2](#0-1) 

At no point is `from` checked against `h.vpnAddrs` (the VPN address(es) actually bound to `h`'s certificate, established during the handshake). Compare this to the handshake path, where `validatePeerCert` explicitly binds `vpnAddrs` to the certificate's `Networks()`: [3](#0-2) 

and to the firewall path, which explicitly checks that a packet's claimed source matches the peer certificate's bound networks (`ErrInvalidRemoteIP`): [4](#0-3) 

No equivalent "does the claimed address belong to this authenticated peer's certificate" check exists in `handleCreateRelayRequest`. As a result, host `h` (holding a valid, CA-signed cert for its own VPN address) can put an arbitrary victim's VPN address in `RelayFromAddr`. The relay node then calls: [5](#0-4) 

which invokes `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` — registering `h`'s own tunnel as the `Established`, `TerminalType` relay destination for the victim's `from` address: [6](#0-5) 

Any subsequent relayed traffic addressed to that victim VPN address that transits this relay node is looked up via `QueryVpnAddrsRelayFor`, which matches purely on the relay-state map keyed by address, with no additional cert check: [7](#0-6) 

and forwarding treats a `TerminalType` match as "I am the target, decrypt/process locally": [8](#0-7) 

so traffic intended for the victim's VPN address is instead delivered into the attacker's tunnel `h`.

### Impact Explanation
An authenticated-but-malicious mesh member (holding only its own legitimately issued certificate — no compromise of the CA or victim key required) can hijack relayed traffic destined for another node's VPN address by falsely claiming ownership of that address in a `CreateRelayRequest`. This is a remote-state-poisoning / traffic-redirection vulnerability: it corrupts the relay node's `RelayState`/`HostMap` so that legitimate relayed packets addressed to the victim are delivered to the attacker's tunnel instead, enabling interception (and potential disruption) of the victim's relayed traffic — directly analogous to the report's "attacker steals resources by supplying someone else's identifier without an ownership check."

### Likelihood Explanation
Exploitation only requires the attacker to be a normal, certificate-holding member of the mesh with a tunnel to a node configured as `relay.am_relay`, and to send a single crafted `Control`/`CreateRelayRequest` message with an arbitrary `RelayFromAddr`. No race condition or privileged position is needed, making this straightforward to trigger in any deployment that uses relays.

### Recommendation
In `handleCreateRelayRequest` (and `handleCreateRelayResponse`/`EstablishRelay` where similar attacker-supplied addresses are trusted), verify that the claimed `from` (`RelayFromAddr`/`OldRelayFromAddr`) address is actually one of `h.vpnAddrs` / bound to `h.GetCert().Certificate.Networks()` before creating or completing any relay state on behalf of that address, mirroring the ownership checks already used in `validatePeerCert` and `firewall.Drop`.

### Proof of Concept
1. Join the mesh as any node `Attacker` with a validly CA-issued certificate for VPN address `A`, and establish a tunnel to a relay node `R` (`relay.am_relay=true`).
2. Craft and send a `Control` message of type `NebulaControl_CreateRelayRequest` over the `Attacker↔R` tunnel with `RelayFromAddr` set to `Victim`'s VPN address (instead of `A`) and `RelayToAddr` set to `R`'s own address.
3. `R.relayManager.handleCreateRelayRequest` sees `target == R`, finds no existing relay state for `Victim` on `Attacker`'s `HostInfo`, and calls `AddRelay(..., h=AttackerHostInfo, vpnIp=Victim, ..., TerminalType, Established)`, marking `Attacker`'s tunnel as the terminal relay for `Victim`.
4. Any peer that subsequently relays traffic through `R` toward `Victim` (e.g., because `R` is a preferred/available relay in `Victim`'s remote list) will have that traffic delivered into `Attacker`'s tunnel via `QueryVpnAddrsRelayFor`/`handleOutsideRelayPacket`'s `TerminalType` branch, instead of reaching `Victim`.

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

**File:** handshake_manager.go (L992-1039)
```go
// validatePeerCert checks the peer certificate for self-connection and remote allow list.
// Returns the VPN addrs, whether any of them fall within one of our own VPN
// networks, and true if valid; false if rejected.
func (hm *HandshakeManager) validatePeerCert(via ViaSender, remoteCert *cert.CachedCertificate) ([]netip.Addr, bool, bool) {
	f := hm.f
	vpnNetworks := remoteCert.Certificate.Networks()

	// The cert package rejects host certs with no networks at parse time, so
	// reaching this state would mean an invariant was bypassed elsewhere.
	// Refuse explicitly so downstream code (which indexes vpnAddrs[0]) can't
	// panic if that invariant ever changes.
	if len(vpnNetworks) == 0 {
		f.l.Info("No networks in certificate",
			"from", via, "cert", remoteCert)
		return nil, false, false
	}

	vpnAddrs := make([]netip.Addr, len(vpnNetworks))
	anyVpnAddrsInCommon := false

	for i, network := range vpnNetworks {
		if f.myVpnAddrsTable.Contains(network.Addr()) {
			f.l.Error("Refusing to handshake with myself",
				"vpnNetworks", vpnNetworks,
				"from", via,
				"certName", remoteCert.Certificate.Name(),
				"certVersion", remoteCert.Certificate.Version(),
				"fingerprint", remoteCert.Fingerprint,
				"issuer", remoteCert.Certificate.Issuer(),
			)
			return nil, false, false
		}
		vpnAddrs[i] = network.Addr()
		if f.myVpnNetworksTable.Contains(network.Addr()) {
			anyVpnAddrsInCommon = true
		}
	}

	if !via.IsRelayed {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(vpnAddrs, via.UdpAddr.Addr()) {
			f.l.Debug("lighthouse.remote_allow_list denied incoming handshake",
				"vpnAddrs", vpnAddrs, "from", via)
			return nil, false, false
		}
	}

	return vpnAddrs, anyVpnAddrsInCommon, true
}
```

**File:** firewall.go (L425-438)
```go
func (f *Firewall) Drop(fp firewall.Packet, incoming bool, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) error {
	// Make sure remote address matches nebula certificate, and determine how to treat it
	if h.networks == nil {
		// Simple case: Certificate has one address and no unsafe networks
		if h.vpnAddrs[0] != fp.RemoteAddr {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
	} else {
		nwType, ok := h.networks.Lookup(fp.RemoteAddr)
		if !ok {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
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

**File:** outside.go (L176-248)
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
		} else {
			hostinfo.logger(f.l).Info("Unexpected target relay state",
				"relayTo", relay.PeerAddr,
				"relayFrom", hostinfo.vpnAddrs[0],
				"targetRelayState", targetRelay.State,
			)
			return
		}
	default:
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Unexpected relay type", "from", via, "relayType", relay.Type)
		}
	}
}
```
