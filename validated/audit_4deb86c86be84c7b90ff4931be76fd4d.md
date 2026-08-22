### Title
Unauthenticated `RelayFromAddr` in `CreateRelayRequest` allows relay/hostmap identity spoofing - (File: relay_manager.go)

### Summary
`relayManager.handleCreateRelayRequest` trusts the peer-supplied `RelayFromAddr` field of an incoming `CreateRelayRequest` control message as the identity of the "source" of a relay, without verifying it matches the actual, cryptographically-authenticated sender (`h.vpnAddrs`). This is the same bug class as the Tapioca H-10 report: a caller-supplied identity/owner parameter is substituted for the verified sender identity in a security-relevant operation, letting an authenticated-but-malicious peer register itself as trusted infrastructure (a relay) for an arbitrary third-party address it does not own.

### Finding Description
When a `CreateRelayRequest` arrives, `relayManager.HandleControlMsg` only validates that `RelayFromAddr`/`RelayToAddr` are non-nil before dispatching to `handleCreateRelayRequest`: [1](#0-0) 

Inside `handleCreateRelayRequest`, the `from` value is taken directly from the untrusted message field `m.RelayFromAddr`, rather than from `h.vpnAddrs[0]` — the address that was actually authenticated for this `HostInfo` `h` during the Noise handshake and certificate verification: [2](#0-1) 

The only identity check performed is that `from` is not one of *my own* addresses (`f.myVpnAddrsTable.Contains(from)`) — there is no check that `from` equals `h.vpnAddrs[0]`, i.e. that the claimed relay-source actually matches the authenticated peer that sent the control message: [3](#0-2) 

When the target of the relay is this node, an entry is registered in `h.relayState` keyed by the attacker-controlled `from` address, and a relay is created/established via `AddRelay`: [4](#0-3) [5](#0-4) 

This mirrors the Tapioca root cause exactly: a function that should use the verified caller identity (`_srcChainSender_` in Tapioca, `h.vpnAddrs[0]` in Nebula) instead consumes an attacker-supplied identity parameter (`remoteTransferMsg_.owner` in Tapioca, `m.RelayFromAddr` in Nebula) when performing a trust-establishing action (recursive transfer / relay registration).

### Impact Explanation
An authenticated-but-malicious peer `h` can send a `CreateRelayRequest` claiming `RelayFromAddr = victim`'s vpn address and `RelayToAddr = me`. Because the code never checks that `from == h.vpnAddrs[0]`, this establishes an `Established`/`Terminal` relay state on `h` for `victim`'s address in my hostmap. Any subsequent decision this node makes to relay traffic destined for `victim` through `h` (relay selection logic keyed by `hostinfo.relayState`/`hm.QueryVpnAddrsRelayFor`) would then route/trust `h` as a legitimate relay endpoint for `victim`, even though `h` never proved control of that identity. This is a remote hostmap/relay-trust poisoning vector: it lets an attacker inject false relay routing state for arbitrary victim addresses without possessing a certificate for that address, potentially diverting or intercepting traffic intended for the victim through the attacker-controlled node.

### Likelihood Explanation
Likelihood is high for any peer that has already completed a normal (legitimate) handshake with the victim node — no certificate for the spoofed address is required, only a valid handshake as *some* peer plus crafting `RelayFromAddr` in a `CreateRelayRequest` control message. This message type is processed automatically as part of ordinary relay control-plane handling (`HandleControlMsg`), requiring no special privileges beyond being an existing peer.

### Recommendation
In `handleCreateRelayRequest` (and analogously in `handleCreateRelayResponse`), require that the identity used as the "from"/source of the relay for `TerminalType` relay creation is bound to the authenticated sender, i.e., verify `from == h.vpnAddrs[0]` (or that `from` is one of `h.vpnAddrs`) before calling `AddRelay`/updating `h.relayState`. Reject the request (log and return) if the claimed `RelayFromAddr` does not match the verified peer identity of `h`, rather than trusting the field as sent.

### Proof of Concept
1. Node `Attacker` completes a legitimate Nebula handshake with `Victim-Target` node, becoming `HostInfo h` on `Victim-Target` with `h.vpnAddrs[0] = Attacker`'s real address.
2. `Attacker` sends a `Control` message of type `NebulaControl_CreateRelayRequest` to `Victim-Target` with:
   - `RelayFromAddr = Victim's` address (an address `Attacker` does not control/own a cert for)
   - `RelayToAddr = Victim-Target`'s own address
3. `Victim-Target.relayManager.HandleControlMsg` → `handleCreateRelayRequest` sees `target == myself`, and because `from` (Victim) is not checked against `h.vpnAddrs[0]` (Attacker), calls `AddRelay(..., h, ..., from=Victim, ..., TerminalType, Established)`, registering `h` (Attacker's hostinfo) as an established relay for `Victim`'s address.
4. `Victim-Target` sends a `CreateRelayResponse` back, completing the relay under the false `Victim` identity, per `handleCreateRelayRequest`'s response path.
5. `Victim-Target` now holds relay state suggesting `Attacker`'s hostinfo can relay traffic for `Victim`, despite `Attacker` never authenticating as `Victim`. [6](#0-5)

### Citations

**File:** relay_manager.go (L227-264)
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
```

**File:** relay_manager.go (L320-341)
```go
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
```

**File:** relay_manager.go (L426-524)
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

		msg, err := resp.Marshal()
		if err != nil {
			logMsg.Error("relayManager Failed to marshal Control CreateRelayResponse message to create relay", "error", err)
		} else {
			f.SendMessageToHostInfo(header.Control, 0, h, msg, make([]byte, 12), make([]byte, mtu))
			rm.l.Info("send CreateRelayResponse",
				"relayFrom", from,
				"relayTo", target,
				"initiatorRelayIndex", resp.InitiatorRelayIndex,
				"responderRelayIndex", resp.ResponderRelayIndex,
				"vpnAddrs", h.vpnAddrs,
			)
		}
		return
```
