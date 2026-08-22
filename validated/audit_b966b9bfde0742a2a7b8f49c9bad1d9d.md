### Title
Terminal relay accepts an attacker-claimed `RelayFromAddr` identity without binding it to the authenticated relay hostinfo, poisoning relay routing state - (File: relay_manager.go)

### Summary
`GnosisSafeRegistry`'s flaw was trusting a self-declared "owner" field inside a structure without verifying the entity that actually controls it, letting an attacker register wallets under any victim address. The analogous pattern in nebula is `relayManager.handleCreateRelayRequest`, which accepts an arbitrary `RelayFromAddr`/`OldRelayFromAddr` field supplied inside an authenticated `Control` message and uses it, unchecked, as the key for the terminal peer's relay-routing table (`relayState`), without verifying that the address actually corresponds to the sender's own certificate identity or to any peer the terminal node has itself authenticated.

### Finding Description
When a node acts as the terminal end of a relay (`target == me`), `handleCreateRelayRequest` derives `from := protoAddrToNetAddr(m.RelayFromAddr)` purely from attacker-controlled message contents sent by the immediate relay peer `h`: [1](#0-0) 

The only check performed is that `from` isn't the terminal node's own address; there is no verification that `from` corresponds to a certificate the terminal node has actually validated, nor that `h` (the authenticated peer that sent the Control message) is legitimately entitled to speak for `from`: [2](#0-1) 

The claimed `from` value is then persisted into `h.relayState` (keyed by IP and index) via `AddRelay`, which populates `relayForByAddr`/`relayForByIdx` on the hostinfo without any additional identity check: [3](#0-2) [4](#0-3) 

This mirrors the Gnosis bug class: a registry/routing table records an "owner"/identity claim taken from a self-controlled field (the Safe's `owners[0]`, here `RelayFromAddr`) rather than from a cryptographically bound source, allowing any authenticated-but-untrusted peer to inject state associating an arbitrary victim identity with itself.

### Impact Explanation
Any peer that can complete a normal (legitimate) handshake with a target node — i.e., any node holding a CA-signed cert, not necessarily malicious — can send a `CreateRelayRequest` claiming to relay for an arbitrary victim `vpnAddr` it does not own. This poisons the terminal node's relay-routing state (`relayForByAddr`) for that victim address, causing subsequent relay lookups (`QueryVpnAddrsRelayFor`) for the victim's address to resolve to the attacker's hostinfo/relay path. Because end-to-end tunnel data is still authenticated via the Noise handshake and per-tunnel keys, this does not by itself decrypt or forge victim traffic, but it does constitute remote state poisoning of the relay hostmap: it can disrupt legitimate relay establishment for the victim (denial of service), and it silently overwrites/collides with any real relay state that node would otherwise register for that victim, since relay state is a shared map keyed only by IP/index with no identity binding to the message sender's own cert.

### Likelihood Explanation
The `CreateRelayRequest`/`CreateRelayResponse` control messages are only nil-checked for the `RelayFromAddr`/`RelayToAddr` fields before being routed to `handleCreateRelayRequest`; there is no requirement that `from` matches the sender's own cert-derived vpnAddr: [5](#0-4) 
Any node capable of establishing a normal handshake (which requires only a CA-signed cert, not special privilege) can reach this code path by sending a forged `CreateRelayRequest`, making the likelihood high for any network where relaying is enabled (`relay.am_relay`), and unauthenticated peers cannot be filtered out beyond the standard cert check.

### Recommendation
Bind the `from` claim to the sender's own authenticated identity when creating terminal relay state, or otherwise verify that `from` is one of the sender's `vpnAddrs` (from `h.GetCert()`), rather than trusting the message field as-is. Where a genuine third-party-forwarded identity is required (`h` legitimately relaying on behalf of another node), require a cryptographic assertion (signed by the actual `from` peer, e.g. as part of its own handshake through the relay) instead of trusting an unauthenticated field embedded in the relay peer's control message.

### Proof of Concept
Not verified against a running exploit; the analysis is based on static review of `relay_manager.go`'s `handleCreateRelayRequest`. A concrete PoC would require: (1) node A (attacker) establishes a normal handshake with terminal node T; (2) A sends `CreateRelayRequest` with `RelayFromAddr` set to victim V's real vpnAddr and `RelayToAddr` set to T's own address; (3) observe that T's hostinfo for A now has `relayState.relayForByAddr[V] != nil`, evidenced by `AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` executing unconditionally for any `from` claimed by A.

### Citations

**File:** relay_manager.go (L320-342)
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

**File:** hostmap.go (L221-226)
```go
func (rs *RelayState) InsertRelay(ip netip.Addr, idx uint32, r *Relay) {
	rs.Lock()
	defer rs.Unlock()
	rs.relayForByAddr[ip] = r
	rs.relayForByIdx[idx] = r
}
```
