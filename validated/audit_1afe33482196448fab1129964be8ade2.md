### Title
Firewall Enforcement Is Inconsistently Applied Between Direct and Relay-Forwarded Message Paths - (File: outside.go)

### Summary
Nebula applies `Firewall.Drop()` allow/deny rule checks on every direct data packet path (both TUN-to-wire and wire-to-TUN), but the relay-forwarding code path in `handleOutsideRelayPacket` re-transmits the decrypted relay payload straight to its target via `SendVia` without ever invoking `Firewall.Drop`. This mirrors the reported bug class of "one code path has the enforcement check, a parallel code path handling the same category of traffic does not."

### Finding Description
For directly-tunneled traffic, both directions are firewall-checked:
- Outbound (TUN → wire): `consumeInsidePacket` calls `f.firewall.Drop(*fwPacket, false, hostinfo, f.pki.GetCAPool(), localCache)` before sending. [1](#0-0) 

For relayed traffic in `ForwardingType` mode, `handleOutsideRelayPacket` authenticates the outer relay frame (via `VerifyRelay`, done earlier in `readOutsidePackets`) and then immediately forwards the still-encrypted inner payload to the target host with `SendVia`, with no call into `f.firewall` anywhere in the function: [2](#0-1) 

Specifically the forwarding branch: [3](#0-2) 

Contrast this with the dispatch for terminal (non-relay) decrypted data, which routes through `readOutsidePackets`'s message-type switch that (for `header.MessageNone`) hands off to the packet-processing function that applies firewall rules the same way `consumeInsidePacket` does on the send side: [4](#0-3) 

The relay-forwarding branch never constructs a `firewall.Packet` from the inner payload and never calls `Firewall.Drop`, so the sending/receiving hosts' `firewall.inbound`/`firewall.outbound` rule tables — the entire access-control layer documented in `firewall.go`'s `Drop` function (`ErrNoMatchingRule`, `ErrInvalidRemoteIP`, etc.) — are structurally bypassed for any traffic that transits through a relay node. [5](#0-4) 

### Impact Explanation
This is a firewall-bypass analog to the reported "withdraw has no pause check while deposit does": the relay data-plane path lacks the same access-control gate that the direct data-plane path enforces. Traffic forwarded through a relay reaches its destination Interface without ever being subjected to `InRules`/`OutRules` matching, `routableNetworks` local-address checks, or `NetworkTypeVPNPeer`/`NetworkTypeUnsafe` restrictions that would otherwise drop it. An operator who has configured strict firewall policy (e.g., allow only specific ports/groups) can have that policy silently circumvented for any tunnel that happens to be relayed, undermining the "Firewall & Allow Lists" security guarantee described in the Nebula overview.

### Likelihood Explanation
Relays are a standard, commonly-enabled feature (`relay.use_relays` / `relay.am_relay`) for NAT traversal, so any deployment using relays is affected whenever traffic actually transits a relay hop — this is not a rare or contrived configuration. No malformed packets or protocol violations are needed; it is simply a missing call in one of two structurally parallel code paths.

### Recommendation
Add a `firewall.Drop` (or equivalent parsed-packet firewall check) call in `handleOutsideRelayPacket`'s `ForwardingType` branch (and/or in the terminal decrypt path when it originated via relay) before calling `SendVia`, so relayed traffic is subject to the same `InRules`/`OutRules` evaluation as directly-tunneled traffic. This requires parsing the inner `signedPayload` with `newPacket`/`firewall.Packet` construction analogous to `consumeInsidePacket`, then rejecting/dropping consistently with the non-relay path.

### Proof of Concept
Not directly executable from the provided index (no test harness code was retrieved that exercises this exact gap), so this is derived purely from static code-path comparison:
1. Configure a Nebula host to relay traffic (`relay.am_relay: true`) between two victim hosts A and B, where A has strict outbound firewall rules that would block a given port from ever transiting a direct tunnel.
2. Establish a relayed tunnel between A and B through the relay (as exercised by `TestRelaysDontCareAboutIps` / relay e2e tests, which only assert reachability, not firewall enforcement). [6](#0-5) 
3. Send traffic from A on the blocked port; because `handleOutsideRelayPacket`'s forwarding branch never calls `Firewall.Drop`, the packet is forwarded to B by the relay without A's or B's firewall rule set being consulted, whereas the same traffic sent over a direct (non-relayed) tunnel would be dropped by `consumeInsidePacket`'s `f.firewall.Drop` call. [7](#0-6) [1](#0-0) 

Note: I could not locate the definition of `handleOutsideMessagePacket` in the indexed content to confirm line-by-line that it calls `Firewall.Drop` for the non-relay terminal path (only its call site was found); this should be verified directly in a full checkout before treating the analysis as fully confirmed.

### Citations

**File:** inside.go (L74-86)
```go
	dropReason := f.firewall.Drop(*fwPacket, false, hostinfo, f.pki.GetCAPool(), localCache)
	if dropReason == nil {
		f.sendNoMetrics(header.Message, 0, hostinfo.ConnectionState, hostinfo, netip.AddrPort{}, packet, nb, out, q)

	} else {
		f.rejectInside(packet, out, q)
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("dropping outbound packet",
				"fwPacket", fwPacket,
				"reason", dropReason,
			)
		}
	}
```

**File:** outside.go (L138-146)
```go
	switch h.Type {
	case header.Message:
		switch h.Subtype {
		case header.MessageNone:
			f.handleOutsideMessagePacket(hostinfo, out, packet, fwPacket, nb, q, localCache)
		default:
			hostinfo.logger(f.l).Error("IsValidSubType was true, but unexpected message subtype seen", "from", via, "header", h)
			return
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

**File:** firewall.go (L423-479)
```go
// Drop returns an error if the packet should be dropped, explaining why. It
// returns nil if the packet should not be dropped.
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
		switch nwType {
		case NetworkTypeVPN:
			break // nothing special
		case NetworkTypeVPNPeer:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrPeerRejected // reject for now, one day this may have different FW rules
		case NetworkTypeUnsafe:
			break // nothing special, one day this may have different FW rules
		default:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrUnknownNetworkType //should never happen
		}
	}

	// Make sure we are supposed to be handling this local ip address
	if !f.routableNetworks.Contains(fp.LocalAddr) {
		f.metrics(incoming).droppedLocalAddr.Inc(1)
		return ErrInvalidLocalIP
	}

	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}

	table := f.OutRules
	if incoming {
		table = f.InRules
	}

	// We now know which firewall table to check against
	if !table.match(fp, incoming, h.ConnectionState.peerCert, caPool) {
		f.metrics(incoming).droppedNoRule.Inc(1)
		return ErrNoMatchingRule
	}

	// We always want to conntrack since it is a faster operation
	f.addConn(fp, incoming)

	return nil
}
```

**File:** e2e/handshakes_test.go (L566-594)
```go
func TestRelaysDontCareAboutIps(t *testing.T) {
	t.Parallel()
	ca, _, caKey, _ := cert_test.NewTestCaCert(cert.Version2, cert.Curve_CURVE25519, time.Now(), time.Now().Add(10*time.Minute), nil, nil, []string{})
	myControl, myVpnIpNet, _, _ := newSimpleServer(cert.Version2, ca, caKey, "me     ", "10.128.0.1/24", m{"relay": m{"use_relays": true}})
	relayControl, relayVpnIpNet, relayUdpAddr, _ := newSimpleServer(cert.Version2, ca, caKey, "relay  ", "2001::9999/24", m{"relay": m{"am_relay": true}})
	theirControl, theirVpnIpNet, theirUdpAddr, _ := newSimpleServer(cert.Version2, ca, caKey, "them   ", "10.128.0.2/24", m{"relay": m{"use_relays": true}})

	// Teach my how to get to the relay and that their can be reached via the relay
	myControl.InjectLightHouseAddr(relayVpnIpNet[0].Addr(), relayUdpAddr)
	myControl.InjectRelays(theirVpnIpNet[0].Addr(), []netip.Addr{relayVpnIpNet[0].Addr()})
	relayControl.InjectLightHouseAddr(theirVpnIpNet[0].Addr(), theirUdpAddr)

	// Build a router so we don't have to reason who gets which packet
	r := router.NewR(t, myControl, relayControl, theirControl)
	defer r.RenderFlow()

	// Start the servers
	myControl.Start()
	relayControl.Start()
	theirControl.Start()

	t.Log("Trigger a handshake from me to them via the relay")
	myControl.InjectTunPacket(BuildTunUDPPacket(theirVpnIpNet[0].Addr(), 80, myVpnIpNet[0].Addr(), 80, []byte("Hi from me")))

	p := r.RouteForAllUntilTxTun(theirControl)
	r.Log("Assert the tunnel works")
	assertUdpPacket(t, []byte("Hi from me"), p, myVpnIpNet[0].Addr(), theirVpnIpNet[0].Addr(), 80, 80)
	r.RenderHostmaps("Final hostmaps", myControl, relayControl, theirControl)
}
```
