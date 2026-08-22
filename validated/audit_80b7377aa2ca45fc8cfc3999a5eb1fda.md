## Title
Unverified `RelayFromAddr` claim in relay Control messages allows an authenticated peer to poison another host's relay routing state - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` accepts the `RelayFromAddr` field of an incoming `CreateRelayRequest` control message and uses it, unverified, as the key under which relay state is installed both on the terminal responder (`h.relayState`) and on the middle-man's peer relay entry (`peer.relayState`). The only check performed is that the claimed `from` address is not the local node's own address; there is no check that `from` actually equals the authenticated sender `h.vpnAddrs[0]` derived from that peer's verified certificate during the handshake. This mirrors the referenced bug class: a value that should be bound to a verified identity (the LP token / the relay "from" address) is instead trusted at face value from an untrusted party, and used to perform state-changing operations (crediting withdrawals / installing relay routes).

### Finding Description
`HandleControlMsg` in `relay_manager.go` receives `h`, the already-authenticated `HostInfo` of the peer that physically sent the control message (its identity was established during the Noise handshake and its certificate verified against the CA pool). The control payload itself, however, carries a separate, peer-supplied `RelayFromAddr`/`RelayToAddr` pair that is fully attacker-controlled: [1](#0-0) 

The only sanity check performed on `from` is that it is not one of the local node's own addresses: [2](#0-1) 

There is no check anywhere in this path that `from == h.vpnAddrs[0]` (i.e., that the claimed originating VPN address actually belongs to the certificate-verified sender `h`). When the local node is the terminal target, the claimed `from` is used directly to install/complete relay state: [3](#0-2) 

When the local node is acting as the middle-man relay, the claimed `from` is likewise used, unverified, to install forwarding relay state on the actual target peer's `relayState`: [4](#0-3) 

This installed state later drives real traffic forwarding decisions in the data path, via `QueryRelayForByIp`/`QueryVpnAddrsRelayFor` and `SendVia`: [5](#0-4) 

Because the relay/hostmap subsystem indexes and dispatches relayed traffic purely by the claimed VPN address (`PeerAddr`) rather than by the certificate identity that was actually verified for the connection that delivered the control message, an authenticated-but-malicious peer can assert an arbitrary `RelayFromAddr` to inject or corrupt relay-routing state for VPN addresses it does not own — exactly the same class of bug as the referenced report, where an unverified value (LP token contract address) is trusted as proof of a prior legitimate operation (staking ETH) instead of being checked against the caller's actual verified state.

### Impact Explanation
An attacker who has a valid, CA-signed certificate for their own identity (e.g. `evilVpnIp`) can send `CreateRelayRequest`/`CreateRelayResponse` messages claiming `RelayFromAddr` equal to a victim's VPN address. Because `from` is never checked against `h.vpnAddrs[0]`, the relay/middle-man installs relay-forwarding state keyed on the victim's address pointing at the attacker's own `HostInfo`/tunnel. This is remote relay-state poisoning: subsequent legitimate relay lookups for the victim's address (`QueryRelayForByIp`, `QueryVpnAddrsRelayFor`) can resolve to state associated with the attacker rather than the victim, letting the attacker intercept, corrupt or blackhole traffic that other hosts believe is being relayed to the victim, without ever needing to compromise the victim's key material.

### Likelihood Explanation
Exploitation only requires that the attacker be a normal, CA-signed member of the mesh who is allowed to act as (or route through) a relay — no host-access or malicious-lighthouse assumption is needed, and the message is a normal, in-protocol `header.Control` packet sent over an already-established, legitimately-authenticated tunnel. The check that exists (`f.myVpnAddrsTable.Contains(from)`) shows the developers were aware `from` needed sanity-checking but only guarded the self-collision case, leaving the sender-identity binding unchecked.

### Recommendation
In `handleCreateRelayRequest` (and correspondingly in `handleCreateRelayResponse`/`EstablishRelay`), require that the initiator-claimed `RelayFromAddr` matches one of the addresses in the verified certificate of the sending `HostInfo` (`h.vpnAddrs`) before using it as a key for `AddRelay`, `CompleteRelayByIP`, or `UpdateRelayForByIpState`. Reject and log any request where the claimed origin does not match the authenticated identity, exactly as is already done for the self-address case.

### Proof of Concept
1. Attacker `E` establishes a normal, CA-signed handshake with relay node `R` (`am_relay: true`), obtaining an authenticated `HostInfo` `h` with `h.vpnAddrs[0] = E`.
2. `E` sends `R` a `CreateRelayRequest` control message with `RelayFromAddr = V` (a victim's VPN address, not `E`'s own) and `RelayToAddr = T` (some reachable target).
3. `R`'s `handleCreateRelayRequest` only checks `f.myVpnAddrsTable.Contains(from)` (false, since `from=V≠R`) and proceeds to call `AddRelay(rm.l, peer, f.hostMap, from, ...)` / `peer.relayState.UpdateRelayForByIpState(from, Requested)`, installing relay-forwarding state for `V` that resolves to the tunnel/session belonging to `E`.
4. Any subsequent relay lookup for VPN address `V` through `R` (`QueryRelayForByIp`/`QueryVpnAddrsRelayFor`) can now return state tied to attacker `E`'s session instead of the legitimate host `V`, letting `E` intercept or disrupt traffic addressed to `V` via `R`.

### Citations

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

**File:** relay_manager.go (L525-553)
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
```

**File:** outside.go (L206-234)
```go
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

		// If that relay is Established, forward the payload through it
		if targetRelay.State == Established {
			switch targetRelay.Type {
			case ForwardingType:
				// Forward this packet through the relay tunnel, rebuilding it in place.
				// Encode overwrites the old outer header, and the new AEAD tag lands where the old one was
				fwdBuf := packet[:0:len(packet)] // Cap to len(packet) to protect memory from a larger parent buffer
				f.SendVia(targetHI, targetRelay, signedPayload, nb, fwdBuf, true)
			case TerminalType:
				hostinfo.logger(f.l).Error("Unexpected Relay Type of Terminal")
				return
			default:
				if f.l.Enabled(context.Background(), slog.LevelDebug) {
					hostinfo.logger(f.l).Debug("Unexpected targetRelay Type", "from", via, "relayType", targetRelay.Type)
				}
				return
			}
```
