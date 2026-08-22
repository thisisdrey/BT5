## Title
Relay control message `RelayFromAddr` is trusted as peer-supplied input instead of being derived from the authenticated peer's certificate — ([File: relay_manager.go])

## Summary
This is a valid analog. In the reported Redacted Cartel bug, `transferBribes` trusted a caller-supplied `fee` parameter instead of using the pre-computed, trusted `fee` state variable, letting an admin manipulate the security-relevant value. In Nebula's relay control-message handling, `handleCreateRelayRequest` derives `from` directly from the attacker/peer-controlled protobuf field `m.RelayFromAddr` (or `m.OldRelayFromAddr`), rather than deriving it from `h.vpnAddrs` — the address that was actually cryptographically bound to the sending peer during the handshake. The already-authenticated identity (`h`, `h.vpnAddrs[0]`) is the trustworthy "calculated" value; `from` is the untrusted "user input" value, and the code never checks that they match.

## Finding Description
`HandleControlMsg` receives a decrypted, authenticated `NebulaControl` message from hostinfo `h` (a peer whose identity was verified via certificate during the handshake, exposed as `h.vpnAddrs`) [1](#0-0) . For `CreateRelayRequest`/`CreateRelayResponse` it only checks that `RelayFromAddr`/`RelayToAddr` are non-nil [2](#0-1) ; it never validates that `RelayFromAddr` equals the sender's own verified `h.vpnAddrs[0]`.

`handleCreateRelayRequest` then unmarshals `from := protoAddrToNetAddr(m.RelayFromAddr)` straight from that unauthenticated-content field and uses it as the key for the relay state it installs on `h`: [3](#0-2) 
and in the branch where `h` is itself acting as the middle-man relay, the same `from` value (fully peer-controlled) is passed to `AddRelay(rm.l, h, f.hostMap, from, ...)` to create/complete relay routing state tied to `h`: [4](#0-3) 
and forwarded onward to a third peer (`peer`) in the "target is not me" branch, again keyed by the attacker-chosen `from`: [5](#0-4) 

The correct, "calculated" value here is `h.vpnAddrs[0]` (the peer's cert-verified overlay address), the analog of the unused `fee` state variable in the original finding. Instead, the code (the analog of `transferBribes`) uses the caller/peer-supplied `RelayFromAddr` field, which has no cryptographic binding to `h`'s actual identity.

## Impact Explanation
Because `from` is never checked against `h.vpnAddrs[0]`, an authenticated-but-malicious peer `h` can send a `CreateRelayRequest` claiming `RelayFromAddr` = an arbitrary victim address it does not own. If `h` is itself the relay target (`f.myVpnAddrsTable.Contains(target)`), this installs relay state on `h` associating an attacker-chosen address with `h`'s connection and returns a `CreateRelayResponse` acknowledging it [6](#0-5) . If `h` is acting as a relay for a third party, the same spoofed `from` is propagated into the `peer`'s relay state and into `AddRelay`'s bookkeeping keyed by that address [7](#0-6) . Because subsequent relay traffic routing depends on hostmap/relay-state lookups keyed by these addresses (`QueryVpnAddrsRelayFor`, `QueryRelayForByIp`), an attacker can poison relay routing state for an address it does not control, a form of remote state poisoning of hostmap/relay trust — matching the reachable-attack categories in scope (hostmap/lighthouse/relay address trust) without requiring a CA-signed certificate belonging to the victim.

## Likelihood Explanation
Any already-connected peer (an authenticated Nebula node, not necessarily a lighthouse or privileged host) can trigger this by sending a `Control`/`NebulaControl_CreateRelayRequest` message with a forged `RelayFromAddr`/`OldRelayFromAddr`. No special timing or race is required — `handleCreateRelayRequest` performs no comparison between the message's claimed `from` and the sender's verified `h.vpnAddrs`.

## Recommendation
In `handleCreateRelayRequest` (and symmetrically in `handleCreateRelayResponse`), reject the message unless `from` (derived from `m.RelayFromAddr`/`m.OldRelayFromAddr`) equals one of the addresses in `h.vpnAddrs` — i.e., replace the trust in the peer-supplied field with the certificate-derived state that is already available on `h`. Add an explicit check immediately after decoding `from` in `handleCreateRelayRequest`:
```go
if !slices.Contains(h.vpnAddrs, from) {
    logMsg.Error("RelayFromAddr does not match sender's verified vpn address", "claimedFrom", from, "actual", h.vpnAddrs)
    return
}
```

## Proof of Concept
1. Establish two legitimate tunnels: `attacker <-> relayNode` and `relayNode <-> victim`, with `relayNode` configured with `relay.am_relay: true`.
2. From `attacker`, send a `Control` message of type `NebulaControl_CreateRelayRequest` over the already-established `attacker<->relayNode` tunnel, setting `RelayFromAddr` to `victim`'s overlay address instead of `attacker`'s own address, and `RelayToAddr` to some third-party target that `relayNode` can reach.
3. Observe that `relayNode.handleCreateRelayRequest` accepts the request because only nil-checks are performed on `RelayFromAddr`/`RelayToAddr` [2](#0-1) , and installs/propagates relay state keyed by the forged `victim` address rather than `attacker`'s real `h.vpnAddrs[0]` [3](#0-2) .
4. This confirms `relayNode`'s relay routing state can be poisoned with an address the sending peer does not own, without presenting any certificate for that address.

*Note: I was unable to fully trace how this misassociated relay state is exploited end-to-end for traffic interception in the data plane within the available search depth (e.g., exact interaction with `QueryVpnAddrsRelayFor` under concurrent legitimate relay setup for the same victim address); a Devin session with full repo/test access would be needed to build a complete runnable e2e PoC and confirm the full exploitation chain.*

### Citations

**File:** relay_manager.go (L298-318)
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
