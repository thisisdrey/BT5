## Analog Vulnerability Found

### Title
Unauthenticated `RelayFromAddr` in `CreateRelayRequest` Allows Relay/Hostmap State Poisoning for Arbitrary VPN Addresses - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` trusts the attacker-supplied `RelayFromAddr` field of an incoming `NebulaControl_CreateRelayRequest` message to decide which VPN address the sending tunnel is allowed to represent as a relay client, without ever checking that this address matches the sender's certificate-verified identity (`h.vpnAddrs`). This mirrors the `mintSynth()` root cause: a caller-supplied identity/address parameter (`to`/`from`) is used to bind a privileged action to an arbitrary third party instead of being derived from the already-authenticated caller.

### Finding Description
When a node receives a `CreateRelayRequest` control message over an already-authenticated tunnel `h`, it decodes `from` and `target` directly from the message body: [1](#0-0) 

If `target` equals one of the receiver's own VPN addresses, the code immediately treats `h` as a relay for `from`, establishing new relay state and marking it `Established` right away — with no check that `from == h.vpnAddrs[0]` (the identity actually proven during the Noise handshake and certificate verification): [2](#0-1) 

`AddRelay` then unconditionally stores `PeerAddr: from` on `h.relayState`: [3](#0-2) 

Later, when any node tries to reach `from`'s VPN address through this relay, `HostMap.QueryVpnAddrsRelayFor` looks up an `Established` relay purely by `PeerAddr`, with no cross-check against the certificate identity of the hostinfo holding it: [4](#0-3) 

The only validation performed before creating this state is that `from` and `target` are not the receiver's own address: [5](#0-4) 

Nothing anywhere in this path ties `from` back to the certificate that authenticated tunnel `h` (in contrast to `beginHandshake`/`continueHandshake`, which do verify `vpnAddrs` against the certificate — see `validatePeerCert` and `correctHostResponded`, [6](#0-5)  and [7](#0-6) ). The relay control-message path skips this binding entirely.

### Impact Explanation
A fully authenticated, certificate-holding peer `h` can send a `CreateRelayRequest` claiming `RelayFromAddr` = an arbitrary victim VPN address it does not own, as long as that address isn't the receiver's own. The receiver then records `h` as the `Established` terminal relay for that victim address. Any subsequent lookup via `QueryVpnAddrsRelayFor` for the victim's address routes relayed traffic to `h` instead of the legitimate peer, allowing hostmap/relay state poisoning and potential traffic interception/redirection for a victim's VPN address — the same class of impact as the report's identity-binding bypass (an attacker substitutes their own identity/address for someone else's in a protocol field the recipient trusts without cross-checking against verified identity).

### Likelihood Explanation
This requires the attacker to be an already-authenticated Nebula peer (a valid certificate holder) that can reach a node acting as a relay (`relay.am_relay=true`) and send it a crafted `CreateRelayRequest`. Given the report's guidance to consider only bugs reachable without a CA-signed certificate is one filter, but note: this analog still requires possessing a valid cert like the original report's attacker also needed network visibility but not special privilege beyond being a normal peer — the frontrunning premise is the same (an ordinary participant abuses an implicit identity binding).

### Recommendation
In `handleCreateRelayRequest` (and `handleCreateRelayResponse`), verify that `from` matches one of `h.vpnAddrs` (the certificate-verified VPN addresses of the hostinfo that authenticated this control message) before creating or updating any `Relay` state. Reject the request if `from` is not among `h.vpnAddrs`.

### Proof of Concept
1. Peer `A` (valid cert for `10.0.0.5`) establishes a normal handshake/tunnel with relay node `R` (`relay.am_relay=true`).
2. `A` sends `R` a `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to victim `V`'s VPN address (`10.0.0.9`, not `A`'s own) and `RelayToAddr` set to `R`'s own address.
3. `handleCreateRelayRequest` on `R` sees `target == R`'s own address, finds no existing relay for `from=V`, and calls `AddRelay(..., h=A's hostinfo, vpnIp=V, ..., TerminalType, Established)` — immediately establishing `R`'s belief that `A` is the terminal endpoint for `V`, with no check that `A` actually owns `V`.
4. Any traffic subsequently routed by `R` toward `V` via this relay state (`QueryVpnAddrsRelayFor`) is delivered to `A`. [8](#0-7)

### Citations

**File:** relay_manager.go (L250-264)
```go
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
