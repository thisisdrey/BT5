### Title
Missing sender-identity binding check in relay `CreateRelayRequest` handling allows a peer to poison relay state for an arbitrary victim address - ([File: relay_manager.go])

### Summary
`relayManager.handleCreateRelayRequest` trusts the self-reported `RelayFromAddr` field inside a `NebulaControl` message as the identity of the peer requesting the relay, without ever checking that this address is one the sending, authenticated `HostInfo` (`h`) actually owns (`h.vpnAddrs`). This mirrors the reported Locker bug: an attacker holds a legitimately signed/authenticated credential (their own cert / smart-wallet signature) but supplies an unrelated reference value (a fake `governor`/here a fake `from` address) that is never cross-checked against the object it is supposed to be bound to (`locker.governor` / here the sender's own `vpnAddrs`).

### Finding Description
`HandleControlMsg` unmarshals the control message and dispatches to `handleCreateRelayRequest` with the already-authenticated `HostInfo` `h` of the sender: [1](#0-0) 

Inside `handleCreateRelayRequest`, the "from" address used to build relay state is taken directly from the message body (`m.RelayFromAddr`), which is fully attacker-controlled plaintext-level protocol data, not derived from the sender's certificate identity: [2](#0-1) 

The only checks performed are that `from` is not one of *my own* addresses and that `target` is one of my own addresses: [3](#0-2) 

There is no check that `from` is contained in `h.vpnAddrs` (the addresses actually certified for `h`, the account that is cryptographically authenticated on this control channel). This is structurally identical to the reported bug: `ApproveProgramLockPrivilege` checks `governor.smart_wallet == smart_wallet` (an internal consistency check on the attacker-supplied `governor` account) but never checks `locker.governor == governor` (that the supplied account is actually bound to the resource in question). Here, the code checks internal consistency of the message (`RelayFromAddr`/`RelayToAddr` non-nil) and that `h` is a valid, authenticated HostInfo, but never binds the claimed identity `from` to `h`'s actual certified identity.

Following this, when `target` is me, relay state is created/updated for `from` and, if none exists, is immediately established as an authenticated terminal relay: [4](#0-3) 

`AddRelay` then inserts this relay association into the hostmap/relayState of the real, authenticated peer `h`, keyed by the attacker-chosen `from` value, with no verification that `from` belongs to `h`: [5](#0-4) 

### Impact Explanation
Any already-connected (but otherwise unprivileged) Nebula peer can send a `CreateRelayRequest` claiming to be relaying traffic "from" an arbitrary victim VPN address it does not own, causing the local node to:
- Register relay/forwarding state (`Relay{PeerAddr: from}`) under the attacker's own `HostInfo` for an address it has no certificate for, and reply with `CreateRelayResponse` establishing that relay as `Established`.
- Collide with or preempt legitimate relay-establishment flows for the real owner of that VPN address (state poisoning of the relay/hostmap subsystem), since relay state per `(HostInfo, peerAddr)` is now attacker-controlled.
- Potentially be used to redirect/absorb relay traffic that legitimate participants intended to route through this node for the victim address, since subsequent legitimate `CreateRelayRequest`/`CreateRelayResponse` traffic for that peer address must reconcile with whatever relay state already exists.

This is a genuine authentication-binding gap reachable by any connected peer without holding the victim's certificate, matching the "remote state poisoning" impact category.

### Likelihood Explanation
Likelihood is high for any node that participates as a potential relay (`am_relay` or simply any node that can be a terminal target of a relay request) and accepts control messages from already-handshaked peers. No special privilege beyond being a normal, authenticated Nebula node is required to send a forged `RelayFromAddr`.

### Recommendation
In `handleCreateRelayRequest` (and correspondingly in `handleCreateRelayResponse`/`EstablishRelay`), verify that the claimed `from` (`RelayFromAddr`) address is actually contained in the sending `HostInfo`'s certified `vpnAddrs` (`h.vpnAddrs`) before creating or updating any relay state on behalf of that address, analogous to requiring `self.locker.governor == self.governor` in the referenced report. Reject the control message if the claimed identity is not one the sender is certified to own.

### Proof of Concept
1. Node `Attacker` establishes a normal, validly certified Nebula tunnel to node `Me` (or to a relay `R` that has a path to `Me`).
2. `Attacker` sends a `NebulaControl` message of type `CreateRelayRequest` with `RelayFromAddr` set to `Victim`'s VPN address (an address `Attacker` does not own/certify) and `RelayToAddr` set to `Me`'s VPN address.
3. `handleCreateRelayRequest` on `Me` sees `target == Me`, and since no existing relay entry exists for `Victim` on `Attacker`'s `HostInfo`, calls `AddRelay(..., h=AttackerHostInfo, vpnIp=Victim, ..., TerminalType, Established)`, creating relay state that treats `Attacker`'s connection as an authorized relay path for `Victim`, and replies with `CreateRelayResponse`.
4. No point in this flow verifies that `Victim` is present in `AttackerHostInfo.vpnAddrs`, so the relay/hostmap state for `Victim` is now influenced by an entity that never proved ownership of `Victim`'s identity. [6](#0-5)

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
