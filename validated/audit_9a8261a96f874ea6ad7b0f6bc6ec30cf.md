Found the analog: in `handleCreateRelayRequest`, the "from" address used for relay-state accounting and authorization is taken entirely from the attacker-controlled `NebulaControl.RelayFromAddr`/`OldRelayFromAddr` field, with no check that it matches the actual VPN address bound to the sending `HostInfo`/certificate. This mirrors the fee-on-transfer class: the accounting system trusts a declared value (`from`) instead of verifying it against the actual, cryptographically-established value (the sender's certificate-issued VPN address).

### Title
Relay control messages trust attacker-declared `RelayFromAddr` instead of the sender's certificate-verified VPN address - (File: relay_manager.go)

### Summary
`relayManager.handleCreateRelayRequest` derives the relay "from" identity solely from the `RelayFromAddr` (or legacy `OldRelayFromAddr`) field inside the `NebulaControl` protobuf message sent by the peer, rather than from `h.vpnAddrs` (the addresses bound to the already-authenticated `HostInfo`/certificate of the sender). This is the same accounting flaw as fee-on-transfer tokens: the system records a caller-supplied value as ground truth without verifying it against the actually-authenticated value.

### Finding Description
`HandleControlMsg` unmarshals the incoming `NebulaControl` and, for `CreateRelayRequest`, forwards to `handleCreateRelayRequest`: [1](#0-0) 

Inside `handleCreateRelayRequest`, the value used for relay bookkeeping (`from`) is taken directly from the message field, not from the caller's actual `HostInfo`: [2](#0-1) 

Only a narrow self-check is performed (`f.myVpnAddrsTable.Contains(from)` to prevent relaying "from myself"); there is no check that `from` matches an address in `h.vpnAddrs` (the addresses that came from the sender's verified certificate during the handshake, e.g. `handshake_manager.go`'s `validatePeerCert`/`vpnAddrs` assignment). As a result, an authenticated peer can declare an arbitrary `from` address in its `CreateRelayRequest`, and the relay node will create/track relay state (`AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)`) and reply with a `CreateRelayResponse` keyed to that attacker-chosen `from` address rather than the sender's real identity: [3](#0-2) 

This is directly analogous to the reported bug class: a value that is supposed to reflect "what was actually verified/received" (here, the sender's true VPN identity) is instead taken from an unverified, attacker-supplied field, and downstream accounting (`relayState`, hostmap `Relays` index, response routing) treats it as authoritative.

### Impact Explanation
Because the relay's internal relay-state table (`h.relayState`, keyed by the declared `from` address) and the `CreateRelayResponse` are built from the unverified `from` value, an authenticated-but-malicious peer can impersonate a third VPN address it does not own when requesting relay service. This can poison relay routing state and hostmap relay indices on the relay node, causing the relay to associate relay slots/state with an address the requester does not control, potentially misdirecting relayed traffic or creating spurious relay entries that are hard to reconcile with the real topology (remote state poisoning).

### Likelihood Explanation
Reachable by any peer that has completed a Nebula handshake with the relay node (i.e., holds a valid CA-signed certificate) — this does not require a lighthouse or hostmap role, only that `relay.am_relay` is enabled on the target. The check performed (`myVpnAddrsTable.Contains(from)`) only prevents a trivial "relay to myself" loop and does nothing to validate the from-address against the sender's own certificate identity, so exploitation requires no special positioning beyond being any peer of the relay.

### Recommendation
When processing `CreateRelayRequest`/`CreateRelayResponse`, validate that the declared `RelayFromAddr`/`OldRelayFromAddr` is actually contained in `h.vpnAddrs` (the certificate-verified addresses of the sending `HostInfo`) before using it for relay-state bookkeeping or response construction. Reject the request if the declared address does not match one of the sender's verified VPN addresses, analogous to checking "declared amount equals actual received amount" in the ERC20 bug class.

### Proof of Concept
1. Establish a normal Nebula handshake between attacker node A (VPN address `A_ip`) and relay node R (with `relay.am_relay: true`).
2. From A, send R a `NebulaControl{Type: CreateRelayRequest, RelayFromAddr: <victim_ip>, RelayToAddr: <target_ip>, InitiatorRelayIndex: idx}` where `victim_ip` is some other node's real VPN address, not `A_ip`.
3. Observe in `handleCreateRelayRequest` that `from := protoAddrToNetAddr(m.RelayFromAddr)` becomes `victim_ip`, and `f.myVpnAddrsTable.Contains(from)` is false (since `victim_ip != R`'s own address), so the check passes and R installs relay state (`AddRelay(...)`) keyed to `victim_ip`, then sends a `CreateRelayResponse` referencing `victim_ip` as the relay-from address — despite A never proving ownership of `victim_ip` via certificate. [4](#0-3)

### Citations

**File:** relay_manager.go (L298-343)
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

**File:** relay_manager.go (L426-509)
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

		resp := NebulaControl{
			Type:                NebulaControl_CreateRelayResponse,
			ResponderRelayIndex: relay.LocalIndex,
			InitiatorRelayIndex: relay.RemoteIndex,
		}

		if v == cert.Version1 {
			b := from.As4()
			resp.OldRelayFromAddr = binary.BigEndian.Uint32(b[:])
			b = target.As4()
			resp.OldRelayToAddr = binary.BigEndian.Uint32(b[:])
		} else {
			resp.RelayFromAddr = netAddrToProtoAddr(from)
			resp.RelayToAddr = netAddrToProtoAddr(target)
		}
```
