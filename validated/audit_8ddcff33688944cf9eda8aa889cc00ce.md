## Title
Malicious peer can self-register as a relay path for an arbitrary third-party VPN address, poisoning relay state - ([File: relay_manager.go])

### Summary
The external report describes a self-registration bug in a Referral contract's `registerReferral`: any caller could register itself into a trust mapping (as a "referrer" for itself), with no verification that the counterpart consented to or is even aware of the relationship, and that forged trust edge was later exploited by the attacker. The reachable analog in Nebula is `relayManager.handleCreateRelayRequest` in `relay_manager.go`, which accepts a `RelayFromAddr`/`RelayToAddr` pair supplied entirely by the remote peer on the `header.Control` channel and inserts it into the local host's authoritative relay-state map (`HostInfo.relayState`) without validating that the claimed "from" address has any actual relationship with the requester.

### Finding Description
`handleCreateRelayRequest` is reached over an already-established (but otherwise untrusted-content) tunnel via `Interface.relayManager.HandleControlMsg` → `handleCreateRelayRequest`: [1](#0-0) 

The function extracts `from` and `target` directly from attacker-controlled protobuf fields (`m.RelayFromAddr`/`m.RelayToAddr`), and if `target` equals my own vpn address, it treats `from` as a genuine peer identity that I should agree to relay for, calling `AddRelay` to install a `Terminal`-type, `Established` relay entry keyed by that self-declared `from` address: [2](#0-1) 

`AddRelay` unconditionally inserts the caller-supplied `vpnIp` (the claimed `from`) into the requesting host's `RelayState.relayForByAddr`/`relayForByIdx` maps, which are the same maps consulted for both bookkeeping and future relay routing decisions: [3](#0-2) [4](#0-3) 

There is no cross-check that `from` corresponds to any VPN address the requester's own certificate (`h`'s `HostInfo.GetCert()`) is entitled to, nor that the party at `from` requested or consented to this relay relationship. The only self-referential check present is for the trivial case where the source of the relay is the caller itself: [5](#0-4) 
which prevents `from == the requester's own address`, but does nothing to stop the requester from claiming an arbitrary *third-party* victim address as `from`.

This is structurally the same defect class as the reported `registerReferral` bug: a party unilaterally inserts itself into a trust/relationship mapping (referrer↔referee in the contract; relay↔peer-address in Nebula) on behalf of an address it does not control, and the system accepts and later acts on that unverified self-declared relationship.

### Impact Explanation
Once the bogus relay entry is installed, the local host's `RelayState` for the requester's `HostInfo` claims a `Terminal`/`Established` relay for the victim's vpn address. This can:
- Poison the local relay-state bookkeeping that `f.hostMap` and `relayManager` rely on when subsequently trying to route to that victim address (e.g., `StartRelays`/`QueryRelayForByIp` consult exactly this map): [6](#0-5) 
- Cause denial-of-service against connectivity to the victim address, since the state a legitimate relay attempt would use is already occupied by an attacker-controlled entry.
- Be leveraged as a building block for traffic-redirection/relay-hijacking abuse, since the relay path decision (`TerminalType` vs `ForwardingType`, and `relay.PeerAddr` lookups in `handleOutsideRelayPacket`) is driven by this attacker-seeded state: [7](#0-6) 

Actual payload confidentiality is still protected by the underlying Noise session/AEAD tied to the real handshake, so this does not directly yield decryption of victim traffic by itself — but it does allow an authenticated-but-malicious peer to insert unverified state about a third party it has no relationship with, mirroring the "no impact beyond bypassing an intended invariant" character of a Medium-severity self-registration bug rather than a full compromise.

### Likelihood Explanation
Any peer that can complete a normal (legitimate) handshake with the target — which requires only a CA-signed certificate, not any special privilege — can immediately send a `NebulaControl_CreateRelayRequest` with an arbitrary `RelayFromAddr` value. No additional capability, timing race, or victim cooperation is required, making this readily reachable by any certificate holder that decides to behave maliciously.

### Recommendation
When handling `CreateRelayRequest` where I am the `target`, verify that the claimed `RelayFromAddr` corresponds to a VPN address actually owned by a certificate/hostinfo relationship the relay-manager can validate (e.g., only accept `from` values that match an address the requester's own certificate can legitimately assert, or require a prior/consenting handshake artifact from the `from` party), instead of trusting the attacker-supplied `from` field verbatim in `handleCreateRelayRequest`/`AddRelay`.

### Proof of Concept
1. Attacker completes a normal handshake with Host M (attacker has a valid, CA-signed cert for its own vpn address `A`).
2. Attacker sends a `header.Control` message of type `NebulaControl_CreateRelayRequest` to M with `RelayFromAddr = V` (an arbitrary victim address the attacker does not own) and `RelayToAddr = M`'s own address.
3. `handleCreateRelayRequest` sees `target == myVpnAddrsTable` is true, sees no existing relay state for `from=V` on the attacker's `HostInfo`, and calls `AddRelay(..., from=V, ..., TerminalType, Established)`, installing `V` into the attacker's `HostInfo.relayState.relayForByAddr[V]` on M — with no verification that `V` is related to the attacker in any way.
4. M's subsequent bookkeeping/routing decisions that consult this `relayState` for `V` are now poisoned by the attacker-controlled entry.

**Note on completeness:** I was not able to trace every downstream consumer of `RelayState.relayForByAddr` (e.g., whether `GetRelaysForMe()`/lighthouse dissemination republishes this attacker-seeded entry to third parties, which would materially strengthen the "poisoning propagates to other victims" impact). This would require further tracing of `lighthouse.go`'s `SendUpdate`/`GetRelaysForMe` path against `hostmap.go`, which I could not complete within the available iterations.

### Citations

**File:** relay_manager.go (L96-108)
```go
		relayHostInfo := rm.hostmap.QueryVpnAddr(relay)
		if relayHostInfo == nil || !relayHostInfo.GetRemote().IsValid() {
			hl.Log(context.Background(), level, "Establish tunnel to relay target", "relay", relay.String())
			f.Handshake(relay)
			continue
		}

		// Check the relay HostInfo to see if we already established a relay through
		existingRelay, ok := relayHostInfo.relayState.QueryRelayForByIp(vpnIp)
		if !ok {
			// No relays exist or requested yet.
			if relayHostInfo.GetRemote().IsValid() {
				idx, err := AddRelay(rm.l, relayHostInfo, rm.hostmap, vpnIp, nil, TerminalType, Requested)
```

**File:** relay_manager.go (L229-264)
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

**File:** relay_manager.go (L480-487)
```go
			}
		} else {
			_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
			if err != nil {
				logMsg.Error("Failed to add relay", "error", err)
				return
			}
		}
```

**File:** hostmap.go (L80-89)
```go
type RelayState struct {
	sync.RWMutex

	relays []netip.Addr // Ordered set of VpnAddrs of Hosts to use as relays to access this peer
	// For data race avoidance, the contents of a *Relay are treated immutably. To update a *Relay, copy the existing data,
	// modify what needs to be updated, and store the new modified copy in the relayForByIp and relayForByIdx maps (with
	// the RelayState Lock held)
	relayForByAddr map[netip.Addr]*Relay // Maps vpnAddr of peers for which this HostInfo is a relay to some Relay info
	relayForByIdx  map[uint32]*Relay     // Maps a local index to some Relay info
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
