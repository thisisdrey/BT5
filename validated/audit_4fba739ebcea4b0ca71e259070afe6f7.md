### Title
Relay-request "from" address is attacker-controlled and unverified against the authenticated sender identity - (File: relay_manager.go)

### Summary
`handleCreateRelayRequest` derives the "relay-from" identity used to key relay state entirely from attacker-supplied message fields (`m.RelayFromAddr` / `m.OldRelayFromAddr`), rather than from the cryptographically-verified peer identity of the `HostInfo` (`h.vpnAddrs`) that authenticated the tunnel the message arrived on. This mirrors the reported bug class: a value used for authorization/state-keying (`contextAddress`-equivalent, here `from`) is set from data that is disjoint from the actual verified sender identity (`msg.sender`-equivalent, here `h`, whose identity was already established and verified during the noise handshake and CA-pool verification).

### Finding Description
`handleCreateRelayRequest` is called with `h *HostInfo`, which is the already-authenticated remote peer (its address, `h.vpnAddrs[0]`, was validated via the CA pool and handshake machine in `handshake_manager.go`, e.g. `validatePeerCert`/`certVerifier`). However, the function pulls the relay's claimed origin address purely from the message payload: [1](#0-0) 

The only self-consistency check performed is `f.myVpnAddrsTable.Contains(from)` (reject if the attacker claims to be *us*), and there is no check that `from == h.vpnAddrs[0]` (i.e., that the claimed relay-from address matches the actual authenticated identity of the sender `h`). This directly parallels the reported analog: `_executeTransaction` sets a `contextAddress` from `transaction.signerAddress`, a value distinct from `msg.sender`, and no check is added preventing a mismatch from being used downstream.

Once accepted, this attacker-chosen `from` value is used to key relay state that is later relied upon for routing decisions: [2](#0-1) 

The resulting relay entry (keyed by the unverified `from`) is later consulted for traffic forwarding via `QueryVpnAddrsRelayFor`/`QueryRelayForByIdx` in the outside packet path: [3](#0-2) 

### Impact Explanation
An authenticated peer (any node holding a valid CA-signed certificate, i.e., no special privilege required beyond normal mesh membership) can register itself as a "relay source" for an arbitrary victim VPN address it does not own, by simply filling in `RelayFromAddr`/`OldRelayFromAddr` with the victim's address rather than its own. Because relay routing state (`relayForByAddr`/`relayForByIdx`, consulted in `handleOutsideRelayPacket`) is keyed on this attacker-supplied address rather than the cryptographically verified peer identity, this could poison relay routing state, potentially causing traffic intended for/from the victim's overlay address to be misrouted or intercepted through the attacker's session on a relay node. This falls under "remote state poisoning" territory described in the validation criteria.

### Likelihood Explanation
Reachable by any peer that has completed a normal handshake with a relay-capable node — no compromised CA key or lighthouse role is required, only a legitimate certificate for some address in the mesh. The relay code path (`handleCreateRelayRequest`) is invoked directly off an authenticated control-channel message, so the attack surface is exercised in ordinary relay-enabled deployments (`relay.am_relay`/`relay.use_relays`).

### Recommendation
In `handleCreateRelayRequest` (and the analogous response/self-checks in the surrounding relay logic), verify that `from` equals the actual authenticated sender's own address, i.e. `h.vpnAddrs[0]` (or one of `h.vpnAddrs`), rather than trusting the message-supplied `RelayFromAddr`/`OldRelayFromAddr` at face value. Reject (or log-and-drop) any `CreateRelayRequest` where the claimed "from" address does not match the verified identity of `h`.

### Proof of Concept
Note: I was unable to fully trace the complete end-to-end forwarding impact (specifically how `SendVia`/`QueryVpnAddrsRelayFor` resolve a mismatched `from` in all downstream cases) within the available search iterations, so the exact blast radius (whether it enables full traffic redirection vs. only relay-state confusion/DoS) is not fully confirmed and should be validated with a live PoC:
1. Node `A` (attacker) completes a normal handshake with relay node `R` (a legitimate CA-signed cert for `A`'s own address).
2. `A` sends `R` a `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to victim `V`'s VPN address (not `A`'s own) and `RelayToAddr` set to some target `T` that `R` also has a tunnel to.
3. `R`'s `handleCreateRelayRequest` accepts this because the only self-check is `f.myVpnAddrsTable.Contains(from)`, which is false (the claimed address is `V`, not `R`).
4. `R` establishes relay state under `h.relayState` (where `h` is `A`'s HostInfo) keyed to address `V`, and communicates this to `T`, associating a relay path for `V`'s address with `A`'s tunnel rather than `V`'s real tunnel.

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

**File:** relay_manager.go (L447-487)
```go
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
