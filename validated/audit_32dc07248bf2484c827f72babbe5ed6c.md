### Title
Relay `CreateRelayRequest` handler trusts an attacker-claimed `RelayFromAddr` identity without binding it to the sender's certificate-verified `vpnAddrs` - ([File: relay_manager.go])

### Summary
`handleCreateRelayRequest` derives the peer identity it registers relay state for (`from`) purely from an attacker-controlled protobuf field in the `NebulaControl` message, rather than from the certificate-bound `vpnAddrs` of the `HostInfo` (`h`) that actually sent the message. This mirrors the root cause of the referenced Arbitrum finding: a "link" between two pieces of state (there, `claimId`/`mutualId`; here, relay identity/address) is validated only loosely (does the field parse, is it not "me"), never checked against the one thing that should be authoritative — the cryptographically-authenticated identity of the entity presenting the claim.

### Finding Description
Nebula binds a peer's identity to `HostInfo.vpnAddrs`, which is populated from the certificate exchanged and verified during the Noise handshake (`handshake_manager.go`, `handshake/machine.go: validateCert`). Every subsequent authenticated action from that `HostInfo` should be attributable only to those `vpnAddrs`.

`relayManager.handleCreateRelayRequest` receives a `NebulaControl_CreateRelayRequest` message over an already-established, authenticated tunnel from `h`, and extracts the claimed source and target of the relay purely from the message body:

```go
func (rm *relayManager) handleCreateRelayRequest(v cert.Version, h *HostInfo, f *Interface, m *NebulaControl) {
	from := protoAddrToNetAddr(m.RelayFromAddr)
	target := protoAddrToNetAddr(m.RelayToAddr)
	...
	if f.myVpnAddrsTable.Contains(from) {
		logMsg.Error("Discarding relay request from myself", "myIP", from)
		return
	}

	if f.myVpnAddrsTable.Contains(target) {
		existingRelay, ok := h.relayState.QueryRelayForByIp(from)
		...
		} else {
			_, err := AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)
``` [1](#0-0) 

The only sanity checks performed on `from` are: (1) it is not one of *my own* vpn addresses, and (2) various index-collision consistency checks once a `RelayState` entry already exists. There is no check that `from` is contained in `h.vpnAddrs` — i.e., that the address the relay request claims to originate from is actually one of the addresses the presenting peer `h` proved ownership of via its certificate. `AddRelay` then unconditionally installs the claimed `from` address into the responder's relay bookkeeping:

```go
func AddRelay(l *slog.Logger, relayHostInfo *HostInfo, hm *HostMap, vpnIp netip.Addr, remoteIdx *uint32, relayType int, state int) (uint32, error) {
	...
	hm.Relays[index] = relayHostInfo
	newRelay := Relay{
		Type:       relayType,
		State:      state,
		LocalIndex: index,
		PeerAddr:   vpnIp,
	}
	...
	relayHostInfo.relayState.InsertRelay(vpnIp, index, &newRelay)
``` [2](#0-1) 

This is structurally the same defect class as the report: a piece of state (relay identity/termination point) is created by trusting a *claimed* linkage (`RelayFromAddr` ⇄ sender) instead of validating it against the one authoritative, cryptographically-verified binding available (`HostInfo.vpnAddrs`, populated only from a CA-signed certificate at handshake time):

```go
type HostInfo struct {
	...
	vpnAddrs []netip.Addr
``` [3](#0-2) 

### Impact Explanation
Any peer holding a valid CA-signed certificate (in scope per the rules — an attacker with no CA-signed certificate cannot even complete a handshake to reach this code path) can send a `CreateRelayRequest` claiming to be relaying traffic for an arbitrary victim `vpnAddr` it does not own. The responder installs `Established`/`Terminal` relay state keyed by that claimed address (`relayForByAddr[from]`, `Relays[index] = h`) without ever verifying `from ∈ h.vpnAddrs`. This lets an authenticated-but-malicious node register itself in another node's relay bookkeeping as the terminal endpoint for an identity it never proved ownership of, which is remote state poisoning of the hostmap/relay tables and can be leveraged to hijack relay routing for a victim's `vpnAddr` on the responder node.

### Likelihood Explanation
Reaching this code requires only a completed Noise handshake with a legitimately signed certificate — no special privileges, no valid-certificate-holder-of-the-target's-identity is needed. The `NebulaControl` message fields (`RelayFromAddr`/`RelayToAddr`) are entirely attacker-supplied and are consumed with no cross-check against the sender's own certificate-derived `vpnAddrs`, making exploitation straightforward for any authenticated peer in the mesh.

### Recommendation
In `handleCreateRelayRequest` (and its `ForwardingType` counterpart, and `handleCreateRelayResponse`), require `from` (and any other self-asserted identity claim in relay control messages) to be a member of `h.vpnAddrs` — i.e., validate the claimed relay-from identity against the certificate-verified identity of the `HostInfo` that actually presented the message, analogous to fixing `checkClaimIdLink` by checking exact linkage rather than mutual/shared identifiers. Reject and log any request where the claimed address is not owned by the authenticated sender.

### Proof of Concept
Conceptual PoC (not executed, environment access unavailable):
1. Stand up three nebula nodes with a shared CA: `attacker`, `relay`, `victim`, where `relay` has `relay.am_relay: true`.
2. `attacker` completes a normal handshake with `relay` and obtains a valid `HostInfo`/tunnel (`h`).
3. `attacker` sends `relay` a `NebulaControl_CreateRelayRequest` with `RelayFromAddr` set to `victim`'s vpn address (not attacker's own) and `RelayToAddr` set to `relay`'s own vpn address.
4. Per `handleCreateRelayRequest`, `relay` only checks that `from` (victim's address) is not its own address, then calls `AddRelay(..., from=victim, TerminalType, Established)`, registering `attacker`'s `HostInfo` as the terminal relay endpoint for `victim`'s identity — despite `attacker` never proving it owns `victim`'s address.
5. Inspect `relay.hostMap.Relays` and `attacker_HostInfo.relayState.relayForByAddr[victimAddr]` to confirm the poisoned entry exists and is `Established`, pointing at the attacker's `HostInfo` instead of at a genuine relay chain terminating at `victim`. [4](#0-3) [5](#0-4)

### Citations

**File:** relay_manager.go (L229-268)
```go
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

**File:** relay_manager.go (L426-493)
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
```

**File:** hostmap.go (L240-251)
```go
type HostInfo struct {
	remote          atomic.Pointer[netip.AddrPort]
	remotes         *RemoteList
	promoteCounter  atomic.Uint32
	ConnectionState *ConnectionState
	remoteIndexId   uint32
	localIndexId    uint32

	// vpnAddrs is a list of vpn addresses assigned to this host that are within our own vpn networks
	// The host may have other vpn addresses that are outside our
	// vpn networks but were removed because they are not usable
	vpnAddrs []netip.Addr
```
