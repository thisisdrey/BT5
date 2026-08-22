## Title
Relay state indexed by attacker-supplied `RelayFromAddr` instead of the authenticated sender's `vpnAddrs` - (File: relay_manager.go)

### Summary
The Otter Audits finding describes a Solana staking bug where `staking_info` is derived and trusted from user-controlled input rather than being explicitly bound to and validated against the authoritative `CVGTStakingPoolState`, letting an attacker spoof the association between the two records. The same bug class — accepting an identity/address field from an untrusted message instead of the cryptographically authenticated peer identity — is reachable in nebula's relay control-message handling.

### Finding Description
`relayManager.handleCreateRelayRequest` derives `from` directly from the wire message field `m.RelayFromAddr` (attacker-controlled payload of a `NebulaControl_CreateRelayRequest`), not from the caller's authenticated `HostInfo` (`h.vpnAddrs`, which is bound to the peer's verified certificate): [1](#0-0) 

The only validation performed in `HandleControlMsg` before dispatch is that `RelayFromAddr`/`RelayToAddr` are non-nil — there is no check that `from` corresponds to `h.vpnAddrs[0]`, the address actually established via the authenticated handshake for that `HostInfo`: [2](#0-1) 

Inside `handleCreateRelayRequest`, when the relay target is “me”, the code looks up/creates relay state keyed by this unverified `from` address (`h.relayState.QueryRelayForByIp(from)` / `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`), and later responds treating `from` as the legitimate identity of the peer requesting the relay: [3](#0-2) 

This mirrors the audited bug precisely: `staking_info` in the Solana contract was accepted/created based on a value tied to the request rather than validated against the authoritative source of truth (`CVGTStakingPoolState`); here, relay state (`RelayState.relayForByAddr`) is created/keyed using a value from the request payload (`RelayFromAddr`) rather than the value nebula already trusts and has verified for that connection — `h.vpnAddrs[0]`, which was populated only after the handshake's certificate verification (`validatePeerCert`, `hostinfo.vpnAddrs = vpnAddrs`) as seen in `handshake_manager.go`: [4](#0-3) 

### Impact Explanation
A connected/authenticated peer `h` (whose own vpn address is fixed by its certificate) can send a `CreateRelayRequest` claiming an arbitrary `RelayFromAddr` unrelated to its own identity. Since `AddRelay`/`InsertRelay` stores this attacker-chosen address as the key in `relayForByAddr` on `h`'s `HostInfo`, this can poison the relay routing table: `HostMap.QueryVpnAddrsRelayFor` later resolves relayed traffic for `targetIps` against whichever `HostInfo` has `relayState` entries for that address, without further re-validating that the entry legitimately belongs to that peer: [5](#0-4) 

This can allow a malicious peer to register itself as a relay for a spoofed source/target VPN address it does not own, enabling forged relay association / traffic misdirection — a remote state-poisoning class of impact analogous to the spoofed `staking_info` linkage in the original report.

### Likelihood Explanation
Reachable by any peer that has completed a handshake (no CA-signed cert needed beyond the attacker's own normal cert) and can then send a crafted `NebulaControl_CreateRelayRequest` — this requires only being a valid mesh member, not compromising any other host, and the only guard is a nil-check on the fields, not an identity-binding check. This is a control-plane message any authenticated peer can send at will.

### Recommendation
Validate that `RelayFromAddr` (and `RelayToAddr` where relevant) equals (or is contained in) `h.vpnAddrs` for the `HostInfo` that sent the control message, rejecting the request if the claimed address does not match the certificate-derived identity, instead of trusting the message's self-reported address field when creating/looking up relay state.

### Proof of Concept
Not independently executable from static analysis alone — this requires running two nebula nodes and crafting a `NebulaControl_CreateRelayRequest` with an `h`-mismatched `RelayFromAddr`/`RelayToAddr` and observing that `relayState`/`hm.Relays` accepts and stores the mismatched association without rejecting it. I could not fully trace end-to-end packet-forwarding consequences (outside.go's relay forwarding path) within the available iterations, so the exact severity of downstream traffic misdirection is not fully confirmed and should be verified with a live e2e test (similar to existing `TestReestablishRelays` / `TestRelaysDontCareAboutIps` in `e2e/handshakes_test.go`).

### Citations

**File:** relay_manager.go (L298-334)
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
```

**File:** relay_manager.go (L426-445)
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

**File:** handshake_manager.go (L881-921)
```go
	vpnNetworks := remoteCert.Certificate.Networks()
	hostinfo.remoteIndexId = result.RemoteIndex
	hostinfo.lastHandshakeTime = result.HandshakeTime

	if !via.IsRelayed {
		hostinfo.SetRemote(via.UdpAddr)
	} else {
		hostinfo.relayState.InsertRelayTo(via.relayHI.vpnAddrs[0])
	}

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
