### Title
Relay identity spoofing via unauthenticated `RelayFromAddr` in `handleCreateRelayRequest` - ([File: relay_manager.go])

### Summary
`relayManager.handleCreateRelayRequest` binds relay state (and, transitively, forwarding trust) to the `RelayFromAddr`/`RelayToAddr` values taken directly from the attacker-controlled `NebulaControl` payload, rather than to the authenticated identity of the sending peer (`h.vpnAddrs`, derived from the handshake-verified certificate). This mirrors the `ExtraordinaryFunding.voteExtraordinary(account_, ...)` flaw, where an arbitrary caller-supplied identity argument was trusted instead of `msg.sender`.

### Finding Description
When node `h` (an already-authenticated peer, verified via the Noise handshake and `CertVerifier`/`CAPool.VerifyCertificate`) sends a `NebulaControl_CreateRelayRequest` control message, the handler extracts the claimed source and target VPN addresses purely from message fields: [1](#0-0) 

`from` here is *not* checked against `h.vpnAddrs` (the actual, certificate-authenticated identity of the sender of this control message). It is attacker-controlled data inside an already-encrypted-and-authenticated tunnel payload, but the payload's *claimed identity* field is never cross-checked against the tunnel's *actual* peer identity — analogous to `voteExtraordinary` trusting the `account_` argument instead of `msg.sender`.

If the target of the relay is me, the code establishes relay state keyed by the attacker-supplied `from` value and immediately marks it `Established` (for a fresh state) or accepts it based on stored `RemoteIndex` matching, associating hostinfo `h` with an arbitrary claimed peer address: [2](#0-1) 

This `Relay{PeerAddr: from}` binding is exactly what later code paths use to decide whose traffic is allowed to flow through the relay tunnel, both for outbound routing lookups (`QueryRelayForByIp`) and for accepting/forwarding inbound relayed frames: [3](#0-2) [4](#0-3) 

Contrast this with the legitimate handshake path, where a peer's VPN address (`vpnAddrs`) is only ever derived from the certificate's `Networks()` after `CertVerifier` succeeds — never from a self-declared field in an unauthenticated sense: [5](#0-4) [6](#0-5) 

The relay-request path breaks this pattern: it never validates that `from` actually corresponds to a VPN address the certificate of `h` is entitled to (i.e., it never checks `from` against `h`'s certificate networks or `h.vpnAddrs`), unlike the handshake code, which strictly derives identity from `remoteCert.Certificate.Networks()`.

### Impact Explanation
An authenticated-but-malicious peer `h` (who has any valid certificate signed by the CA, but is not on the CA-signed victim's cert) can send `CreateRelayRequest` messages claiming `RelayFromAddr` equal to a victim's VPN address it does not own. If accepted, this poisons the relay's `RelayState` (`relayForByAddr`) to treat `h`'s tunnel as though it is relaying traffic *for* the victim's identity. This is remote state poisoning of the relay/hostmap trust structure: a node can impersonate another node's VPN address in the relay-request protocol without needing to hold that node's certificate/private key, since the field is taken from the wire, not from the authenticated certificate that established the tunnel. Depending on how outbound firewall/data-plane logic later trusts `Relay.PeerAddr` bindings for forwarding decisions, this can enable traffic to be misattributed/misrouted through the relay under a spoofed identity, undermining the certificate-based trust model that firewall rules (`FirewallRule.match` on `c.Certificate.Name()`/groups) and `Firewall.Drop`'s remote-IP check depend on.

### Likelihood Explanation
Likelihood is Medium: the attacker need only be an authenticated peer capable of establishing any tunnel to a relay node (`am_relay: true`) — no CA-signed certificate matching the victim's identity is required, satisfying the "no CA-signed certificate for the target identity" reachability constraint. The control message (`header.Control`) is processed on every completed tunnel without any additional check that `RelayFromAddr` matches the sender's own certificate networks, so exploitation requires only crafting a `NebulaControl_CreateRelayRequest` with a chosen `RelayFromAddr`.

### Recommendation
- **Short term**: In `handleCreateRelayRequest` (and `handleCreateRelayResponse`), validate that `from` (`RelayFromAddr`) is contained within `h`'s own certificate-derived VPN networks (`h.GetCert().Certificate.Networks()` / `h.vpnAddrs`) before creating or updating any `RelayState` entry, exactly as `validatePeerCert` and `continueHandshake` already do for handshake-derived identity. Reject the request if the claimed identity does not match the authenticated peer.
- **Long term**: Develop and enforce protocol-wide invariants that any peer-identity value carried inside an application-layer message (control, lighthouse, relay) must be checked against the certificate-authenticated identity of the tunnel it arrived on before being used to establish trust relationships, and add invariant/fuzz tests specifically targeting this class of "trusted caller-supplied identity" bug across `relay_manager.go` and `lighthouse.go`.

### Proof of Concept
1. Attacker node `M` obtains a valid certificate from the same CA (e.g. as any legitimate participant) and establishes a normal tunnel to relay node `R` (`am_relay: true`).
2. `M` crafts and sends a `NebulaControl_CreateRelayRequest` message over this authenticated tunnel with `RelayFromAddr = victimVpnAddr` (an address `M` does not own) and `RelayToAddr = R`'s own VPN address.
3. `relayManager.handleCreateRelayRequest` on `R` sees `f.myVpnAddrsTable.Contains(target)` is true, finds no existing relay state for `from = victimVpnAddr`, and calls:
```go
_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
```
binding `R`'s relay state for identity `victimVpnAddr` to `M`'s hostinfo `h`, and responds with `NebulaControl_CreateRelayResponse` completing the relay — all without ever checking that `M`'s certificate actually contains `victimVpnAddr` in its `Networks()`.

Note: I was unable to fully trace every downstream consumer of `Relay.PeerAddr` bindings across the full data plane (e.g., all firewall interactions with relay-forwarded packets) within the available index; a Devin session with full repository access would be needed to confirm the complete end-to-end exploitability (e.g., whether this spoofed binding can be leveraged to also spoof the *source* of forwarded data packets bypassing firewall source-IP checks), but the identity-trust flaw itself — accepting an unauthenticated, caller-supplied VPN-address claim as if it were the authenticated peer identity — is confirmed directly in the code shown above.

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

**File:** hostmap.go (L207-212)
```go
func (rs *RelayState) QueryRelayForByIp(vpnIp netip.Addr) (*Relay, bool) {
	rs.RLock()
	defer rs.RUnlock()
	r, ok := rs.relayForByAddr[vpnIp]
	return r, ok
}
```

**File:** outside.go (L176-216)
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
```

**File:** handshake_manager.go (L746-750)
```go
	// Validate peer identity
	vpnAddrs, anyVpnAddrsInCommon, ok := hm.validatePeerCert(via, remoteCert)
	if !ok {
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
