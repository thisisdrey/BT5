Confirmed: `HandleControlMsg` only checks that `RelayFromAddr`/`RelayToAddr` are non-nil — it never verifies that the claimed `RelayFromAddr` matches the certificate-bound `vpnAddrs` of the authenticated peer `h` that actually sent the control message. [1](#0-0) 

### Title
Relay address-trust bypass: `handleCreateRelayRequest` accepts attacker-controlled `RelayFromAddr` without binding it to the sender's certificate identity - (File: relay_manager.go)

### Summary
`DstSwapper._processTx` inflates deposits because it credits a swap purely from an unauthenticated before/after balance delta, never checking that the claimed origin of the value matches the party that actually supplied it — allowing repeated relabeling of the same liquidity across swaps for the same payload. Nebula's `relayManager.handleCreateRelayRequest` has the same class of defect at the hostmap/relay trust layer: it builds and installs relay routing state keyed by a `from` (`RelayFromAddr`) address taken verbatim from the incoming `NebulaControl` message, without ever checking that `from` corresponds to the certificate-authenticated `vpnAddrs` of the `HostInfo` (`h`) that actually delivered the message.

### Finding Description
`HandleControlMsg` unmarshals the control payload and only rejects messages where `RelayFromAddr`/`RelayToAddr` are `nil`; it performs no identity check tying `RelayFromAddr` to `h`, the already-authenticated tunnel this control message arrived on. [2](#0-1) 

`handleCreateRelayRequest` then uses this unvalidated `from` value to:
- Look up/insert relay state on the terminal host keyed by `from`, and transition it through `Requested`/`Established`/`Disestablished` states based solely on index bookkeeping, never on a proof that `h` is `from`. [3](#0-2) 
- On the forwarding (middle-relay) path, allocate a new `ForwardingType` relay entry on `peer` (the real target) keyed by the caller-supplied `from`, and forward a `CreateRelayRequest` toward `peer` on behalf of that claimed `from` address — again with no check that `h` owns `from`. [4](#0-3) 

The only self-referential check present is that `from` isn't one of *my own* addresses (`f.myVpnAddrsTable.Contains(from)`), which prevents impersonating the local node but does nothing to stop `h` from impersonating a *third* legitimate peer's `vpnAddrs`. [5](#0-4) 

This mirrors the `DstSwapper` root cause precisely: a state-mutating operation (installing relay routing/forwarding state, analogous to crediting a swap) is driven by attacker-supplied metadata (`RelayFromAddr`, analogous to the swap's claimed token/amount) instead of a value derived from and verified against the authenticated party performing the operation. Just as `DstSwapper` never checks that the "increase" it credits is actually backed by a corresponding decrease from the real owner, Nebula's relay layer never checks that the "from" identity it installs routing state for is actually backed by the certificate identity of the connection it arrived on.

### Impact Explanation
An already-authenticated (but otherwise unprivileged, non-CA-signing) peer can send crafted `CreateRelayRequest` control messages claiming an arbitrary `RelayFromAddr` belonging to a different, legitimate node in the mesh. This can poison relay/hostmap routing state (`RelayState.relayForByAddr` / `relayForByIdx`) on the terminal target or on intermediate relays, inserting entries that associate a victim's vpn address with the attacker's own hostinfo/index. Depending on how the resulting `Established` relay entries are later consulted for forwarding decisions (`QueryRelayForByIp`/`QueryRelayForByIdx`, used throughout `SendVia`/`sendNoMetrics`), this can redirect or duplicate relayed traffic intended for the impersonated address, i.e. remote relay-state poisoning that can be leveraged toward traffic misdirection through relays the attacker does not legitimately own a path to.

### Likelihood Explanation
Any node that already has a working, authenticated tunnel to a relay-capable peer can trigger this: it requires only sending an unsolicited `Control`/`CreateRelayRequest` message with a forged `RelayFromAddr`, which is fully attacker-controlled wire data. No CA-signed certificate for the impersonated address is required — the attack only needs the attacker's own valid certificate to establish the initial tunnel to the relay. This makes the likelihood high for any deployment that enables relays (`relay.use_relays` / `relay.am_relay`).

### Recommendation
Bind `RelayFromAddr` to the authenticated identity of the connection the control message arrived on:
- When `h` is the direct/originating hop, require `from == h.vpnAddrs[0]` (or that `from` is one of `h`'s certificate-bound addresses) before creating or completing any `RelayState` entry.
- For the multi-hop/forwarding case, since a middle relay legitimately forwards on behalf of a third party, require that the *previous* hop's control message chain be verifiable (e.g., embed/require a certificate-backed attestation of `from`, or restrict `from` to addresses that hop has already authenticated relay-state for), rather than trusting a bare address field supplied by any hop.
- As defense in depth, rate-limit/cap the number of distinct `from` identities a single `HostInfo` may register relay state for, to bound the blast radius of a still-possible forgery.

### Proof of Concept
1. Attacker `A` establishes a normal, certificate-authenticated tunnel to relay-capable node `R` (`relay.am_relay=true`), and a normal tunnel exists between `R` and victim `V`.
2. `A` sends `R` a `Control` message of type `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to `V`'s vpn address (not `A`'s own) and `RelayToAddr` set to some `target` that `R` also has a tunnel to.
3. `HandleControlMsg` on `R` passes the message through (only checks for non-nil addrs) and calls `handleCreateRelayRequest`, which — because `target != R`'s own address — takes the forwarding branch, calls `AddRelay(..., from=V's address, ForwardingType, Requested)` against `peer` (the real `target` node), and sends a `CreateRelayRequest` onward claiming `RelayFromAddr = V`.
4. `R`'s relay state for `peer` is now populated under `V`'s identity but wired to `A`'s control channel/index, with no check anywhere in the path that `A` actually is `V`. Subsequent relay traffic bookkeeping (`Established`/`Disestablished` transitions, `QueryRelayForByIp(V)`) can now be manipulated by `A` alone, without `V`'s participation or knowledge. [4](#0-3)

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

**File:** relay_manager.go (L438-444)
```go
	logMsg.Info("handleCreateRelayRequest")
	// Is the source of the relay me? This should never happen, but did happen due to
	// an issue migrating relays over to newly re-handshaked host info objects.
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}
```

**File:** relay_manager.go (L446-487)
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
```

**File:** relay_manager.go (L525-604)
```go
	} else {
		// the target is not me. Create a relay to the target, from me.
		if !rm.GetAmRelay() {
			return
		}
		peer := rm.hostmap.QueryVpnAddr(target)
		if peer == nil {
			// Try to establish a connection to this host. If we get a future relay request,
			// we'll be ready!
			f.Handshake(target)
			return
		}
		if !peer.GetRemote().IsValid() {
			// Only create relays to peers for whom I have a direct connection
			return
		}
		var index uint32
		var err error
		targetRelay, ok := peer.relayState.QueryRelayForByIp(from)
		if ok {
			index = targetRelay.LocalIndex
		} else {
			// Allocate an index in the hostMap for this relay peer
			index, err = AddRelay(rm.l, peer, f.hostMap, from, nil, ForwardingType, Requested)
			if err != nil {
				return
			}
		}
		peer.relayState.UpdateRelayForByIpState(from, Requested)
		// Send a CreateRelayRequest to the peer.
		req := NebulaControl{
			Type:                NebulaControl_CreateRelayRequest,
			InitiatorRelayIndex: index,
		}

		if v == cert.Version1 {
			if !h.vpnAddrs[0].Is4() {
				rm.l.Error("Refusing to CreateRelayRequest for a v1 relay with an ipv6 address",
					"relayFrom", h.vpnAddrs[0],
					"relayTo", target,
					"initiatorRelayIndex", req.InitiatorRelayIndex,
					"responderRelayIndex", req.ResponderRelayIndex,
					"vpnAddr", target,
				)
				return
			}

			b := h.vpnAddrs[0].As4()
			req.OldRelayFromAddr = binary.BigEndian.Uint32(b[:])
			b = target.As4()
			req.OldRelayToAddr = binary.BigEndian.Uint32(b[:])
		} else {
			req.RelayFromAddr = netAddrToProtoAddr(h.vpnAddrs[0])
			req.RelayToAddr = netAddrToProtoAddr(target)
		}

		msg, err := req.Marshal()
		if err != nil {
			logMsg.Error("relayManager Failed to marshal Control message to create relay", "error", err)
		} else {
			f.SendMessageToHostInfo(header.Control, 0, peer, msg, make([]byte, 12), make([]byte, mtu))
			rm.l.Info("send CreateRelayRequest",
				"relayFrom", h.vpnAddrs[0],
				"relayTo", target,
				"initiatorRelayIndex", req.InitiatorRelayIndex,
				"responderRelayIndex", req.ResponderRelayIndex,
				"vpnAddr", target,
			)
		}

		// Also track the half-created Relay state just received
		_, ok = h.relayState.QueryRelayForByIp(target)
		if !ok {
			_, err := AddRelay(rm.l, h, f.hostMap, target, &m.InitiatorRelayIndex, ForwardingType, PeerRequested)
			if err != nil {
				logMsg.Error("relayManager Failed to allocate a local index for relay", "error", err)
				return
			}
		}
	}
```
