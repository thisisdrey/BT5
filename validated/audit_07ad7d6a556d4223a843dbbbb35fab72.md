Confirmed: `HandleControlMsg` only validates that `RelayFromAddr`/`RelayToAddr` are non-nil — it never checks that `RelayFromAddr` (the claimed identity of the relay initiator) matches the certificate-verified `h.vpnAddrs` of the actual sender of the control message. This is the same bug class as the Teller `M-10` finding: a value that names a "resource" (there, a `marketId`; here, a `from`/`RelayFromAddr` vpn address) is accepted and used to build persistent state without validating that it corresponds to something real/authorized, and that state can never be cleaned up through the normal protocol path.

### Title
Unauthenticated `RelayFromAddr` in `CreateRelayRequest` lets a peer register permanent forwarding-relay state for an arbitrary, uninvolved VPN address - (File: relay_manager.go)

### Summary
`relayManager.handleCreateRelayRequest` trusts the `RelayFromAddr` field taken from the wire-level `NebulaControl` message as the identity of the relay's initiator, without ever checking it against the certificate-verified `vpnAddrs` of `h` (the `HostInfo` that actually sent/authenticated the control message).

### Finding Description
`HandleControlMsg` unmarshals the control message and only checks that `RelayFromAddr`/`RelayToAddr` are non-nil: [1](#0-0) 

It then dispatches straight into `handleCreateRelayRequest`, which derives `from` purely from the attacker-controlled `m.RelayFromAddr` field: [2](#0-1) 

The only sanity check performed on `from` is that it isn't one of *my* own addresses (`f.myVpnAddrsTable.Contains(from)`); there is no check that `from == h.vpnAddrs[0]` (i.e., that the claimed relay source is the actually-authenticated peer who sent the message). In the "I am the middleman" branch, this unauthenticated `from` is used to look up/allocate relay state on `peer` and is echoed back into a `CreateRelayRequest` forwarded to `peer`: [3](#0-2) 

and `AddRelay` unconditionally creates a `Relay` entry keyed by this unauthenticated address with state `PeerRequested`/`Requested`: [4](#0-3) 

This mirrors the Teller M-10 root cause exactly: a caller supplies an identifier (`marketId` there, `RelayFromAddr` here) referencing an entity that is never validated to correspond to the caller's own authenticated identity/resource, so state gets created that references something that either doesn't exist or wasn't actually requested by its true owner. Just as the Teller bid became permanently stuck (0% APY, `bidDefaultDuration == 0`, and division by zero on repay) with no code path to unwind it, a relay entry created for a spoofed `from` address sits in `PeerRequested`/`Requested` state on `peer` and `h.relayState` with no natural trigger to transition it to `Established` (the real address never sends the corresponding `CreateRelayRequest`), and no explicit cleanup path exists for this specific case (relay cleanup ties to hostinfo teardown, not to spoofed/unused relay entries pointing at addresses uninvolved in any real tunnel).

### Impact Explanation
An authenticated-but-malicious peer `h` (who has a valid CA-signed cert, but is otherwise untrusted for third-party addresses) can:
- Force a relay node to allocate and retain `Relay` map/index entries and `relayState` bookkeeping for arbitrary victim VPN addresses that never asked to be relayed through this path, wasting relay-node hostmap/index slots (`hm.Relays`, `relayForByAddr`, `relayForByIdx`) that are a limited, indexed resource (32-attempt unique-index generation in `AddRelay`).
- Poison `peer.relayState` and `h.relayState` with stuck `Requested`/`PeerRequested` entries for addresses that are not actually trying to establish a relay through this path, since nothing in the flow re-validates that the claimed "from" party genuinely initiated this relay attempt.
- Repeatedly do this for many distinct spoofed `from` addresses to exhaust relay index slots on a relay node (`am_relay: true`), degrading or denying relay service for legitimate peers - a remote resource-exhaustion / partial denial-of-service impact reachable purely with a valid handshake-completed session and no special privileges toward the spoofed target.

This is capped by Medium severity in spirit to the original finding: it requires the attacker to already be an authenticated peer (not a pre-auth attacker), and by itself doesn't decrypt/forge data-plane traffic, but it does let one authenticated node inject state on behalf of unrelated third parties without their consent, and that state has no clear self-healing path in the reviewed code.

### Likelihood Explanation
Likelihood is Medium: it requires an attacker to have completed a legitimate handshake with a node running as a relay (`relay.am_relay = true`), which is a normal, expected mesh topology in networks using Nebula relays. No certificate forgery or CA compromise is needed — only sending a crafted `NebulaControl_CreateRelayRequest` (header type `Control`) with an arbitrary `RelayFromAddr`, which is trivial once a tunnel exists.

### Recommendation
In `handleCreateRelayRequest` (and `handleCreateRelayResponse`), require that `from` (`RelayFromAddr`/`OldRelayFromAddr`) equal one of `h.vpnAddrs` — i.e., verify the claimed relay-initiator address matches the certificate-verified identity of the `HostInfo` that actually sent the control message — before creating or mutating any `Relay`/`relayState` entries. Reject and drop the message (with a debug/error log, similar to the existing nil-address checks in `HandleControlMsg`) if `from` does not match the sender's authenticated VPN address(es).

### Proof of Concept
1. Attacker `A` completes a normal, valid handshake with relay node `R` (which has `relay.am_relay = true`), so `R` has a `HostInfo` `h` for `A` with `h.vpnAddrs = [A]`.
2. `A` sends `R` a `Control` packet carrying a `NebulaControl_CreateRelayRequest` with `RelayFromAddr = V` (an arbitrary victim address, not `A`) and `RelayToAddr = T` (some peer `R` is already connected to).
3. `relayManager.HandleControlMsg` on `R` only checks that `RelayFromAddr`/`RelayToAddr` are non-nil, then calls `handleCreateRelayRequest(v, h, f, msg)` with `from = V`, `target = T`.
4. Since `target != R`'s own address, `R` (being `am_relay`) looks up `peer = hostmap.QueryVpnAddr(T)`. If `peer` exists and has a valid remote, `R` calls `AddRelay(rm.l, peer, f.hostMap, from /* = V */, nil, ForwardingType, Requested)` and sends a `CreateRelayRequest` to `T` naming `V` as the relay-from address, and also inserts a `PeerRequested` relay entry on `h` (`A`'s HostInfo) for target `T` — all without ever verifying that `V` is `A`'s own address.
5. `T` now holds a `Requested` relay state pointing at `V`, which `V` never asked for and may not resolve; `R`'s `hm.Relays`/`relayState` maps also carry unresolved entries tied to `V`. Repeating with many distinct spoofed `V` values consumes relay-node bookkeeping/index slots and creates stuck relay states with no legitimate initiator to complete them.

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

**File:** relay_manager.go (L320-334)
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
