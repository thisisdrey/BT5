## Analysis

The reported bug class is: *an authenticated identity's permission check does not extend to a second, separately-specified identity carried in the same request, so the "trusted" action is actually performed on behalf of an unverified party*. The strongest reachable analog for this in `Kohvert/nebula--025` is in the relay control-message handling.

### Title
Relay trust state is keyed by attacker-supplied `RelayFromAddr`/`RelayToAddr` fields instead of the cryptographically authenticated peer identity - (`File: relay_manager.go`)

### Finding Description
`relayManager.HandleControlMsg` is invoked for `NebulaControl` messages received over an already-established, certificate-authenticated tunnel (`h *HostInfo`, whose `vpnAddrs` were verified during the handshake). It unmarshals the message and dispatches to `handleCreateRelayRequest`: [1](#0-0) 

Inside `handleCreateRelayRequest`, the addresses used to establish relay trust are taken directly from the message body (`m.RelayFromAddr` / `m.RelayToAddr`), not from `h.vpnAddrs` (the cryptographically verified identity of the sender): [2](#0-1) 

When the claimed target is this node, the code registers relay state keyed by the attacker-controlled `from` value, binding it to the authenticated connection `h`: [3](#0-2) 

This mirrors the reported pattern precisely: the code verifies *who* is talking to it (`h`, analogous to `msg.sender` being KYC'd), but grants the trust/authorization action (`AddRelay` state establishment, `Established` relay records) for a *different, self-declared identity* (`from`, analogous to the NFT's `to` parameter) that was never checked against `h`'s actual certificate-verified `vpnAddrs`. A peer with a legitimate certificate for address `X` can send a `CreateRelayRequest` claiming `RelayFromAddr = Y` (any other real peer's VPN address it wants to impersonate for relay bookkeeping purposes), and the node will create/complete relay state associating connection `h` with `Y` as if `Y` had actually requested/established that relay.

### Impact Explanation
Because relay lookups elsewhere (e.g., `QueryRelayForByIp`, `CompleteRelayByIP`) key off this attacker-supplied address rather than the verified `h.vpnAddrs[0]`, an authenticated-but-malicious peer can poison another node's relay state table with entries for victim VPN addresses it does not own. This can be used to:
- Desynchronize/confuse legitimate relay establishment for the victim address (denial of service against the victim's relay path), since the state machine (`Requested`/`Established`/`Disestablished`) for `from` gets driven by an unrelated, unauthenticated connection.
- Potentially cause the relay subsystem to treat subsequent relayed traffic tagged with the victim's address through the attacker's connection as legitimate relay traffic for that victim, since the state ("Established") that later code consults is not cross-checked against the actual holder of that certificate.

I was not able to fully trace how `VerifyRelay` in `connection_state.go` (only a partial view was available: `func (cs *ConnectionState) VerifyRelay(...)`) cross-checks decrypted relay-forwarded packet contents against this address-keyed state, so I cannot conclusively state whether this reaches full traffic decryption/forgery, or is limited to state-table poisoning/DoS against the relay negotiation. This is a genuine gap in my verification.

### Likelihood Explanation
Reachable by any peer that can complete a normal handshake (i.e., any holder of a certificate signed by a CA in the pool, not a privileged node) — no special "malicious lighthouse/relay" role is required, since the vulnerable code path is entered by any inbound `NebulaControl` message on a normal tunnel. The `RelayFromAddr`/`RelayToAddr` fields are fully attacker-controlled wire fields with no cryptographic binding to the sender's certificate.

### Recommendation
`handleCreateRelayRequest`/`handleCreateRelayResponse` should derive the "from" identity from the authenticated `h.vpnAddrs` (the cert-verified identity of the connection the control message arrived on) rather than trusting the `RelayFromAddr` field taken from the message body. If the protocol legitimately needs the initiator to specify a different `from` address (e.g., for forwarding-type relays), that value must be validated to actually correspond to `h`'s certificate-verified VPN address set before any relay state is created or transitioned to `Established`.

### Proof of Concept
Conceptual (not verified end-to-end due to tool-call limits):
1. Attacker `X` completes a normal, valid handshake with victim node `N` (attacker has a legitimately signed cert for VPN address `X`).
2. Attacker sends `NebulaControl{Type: CreateRelayRequest, RelayFromAddr: <victim Y's real VPN address>, RelayToAddr: <N's own VPN address>}` over the `X`↔`N` tunnel.
3. `N.handleCreateRelayRequest` sees `target == N`'s own address, and because no existing relay state exists for `from = Y`, calls `AddRelay(rm.l, h, f.hostMap, Y, &m.InitiatorRelayIndex, TerminalType, Established)` — creating an `Established` relay record for `Y` bound to connection `h` (which is actually `X`), despite `Y` never having sent this request or authenticated on this connection. [4](#0-3) 

Because I could not fully confirm downstream consumption of this poisoned relay-state entry within the tool-call budget, treat the "Established relay-state poisoning for an address the sender does not control" as the concretely proven root cause, and the traffic-forgery escalation as a plausible but unverified extension that should be checked directly against `connection_state.go`'s `VerifyRelay` and the relay forwarding path in `outside.go`.

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
