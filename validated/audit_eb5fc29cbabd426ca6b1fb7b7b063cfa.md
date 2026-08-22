## Title
Missing cross-validation of `RelayFromAddr`/`from` against the sender's certificate-verified identity in `relayManager.handleCreateRelayRequest()` allows relay-state address spoofing - (File: relay_manager.go)

## Summary
`relayManager.HandleControlMsg()` decodes a `NebulaControl` message received over an already-authenticated tunnel (`h *HostInfo`, whose identity was established and certificate-verified during the handshake) and passes the attacker-controlled `RelayFromAddr` field straight into `handleCreateRelayRequest()` as `from`. The code never checks that `from` actually equals one of `h.vpnAddrs` — the VPN address(es) that were cryptographically bound to `h` by its certificate. This is the same bug class as the reported `LPExternalRequestsManager._completeBurn()` issue: a value taken from an untrusted/peer-supplied message (`withdrawalToken` / `RelayFromAddr`) is used to drive privileged logic without validating it matches the authoritative value (`request.token` / `h.vpnAddrs`).

## Finding Description
`relay_manager.go`'s `handleCreateRelayRequest` is invoked from `HandleControlMsg`, which is reached from `outside.go`'s `readOutsidePackets` (`case header.Control: f.relayManager.HandleControlMsg(hostinfo, out, f)`), i.e. from any already-handshaked, certificate-verified peer: [1](#0-0) 

Inside `handleCreateRelayRequest`, `from` is decoded purely from the message body sent by the peer: [2](#0-1) 

The only identity check performed is whether `from` happens to be *my own* address (`f.myVpnAddrsTable.Contains(from)`), never whether `from` matches the sending peer's own certificate-bound address (`h.vpnAddrs`). When "the target of the relay" (`m.RelayToAddr`) is me, this unchecked `from` value is used to create/lookup relay state keyed by that address: [3](#0-2) 

The same unvalidated `from` is used again in the "I am the intermediate relay" branch to key `AddRelay`/`QueryRelayForByIp` against the peer whose target the request names: [4](#0-3) 

Contrast this with `handshake_manager.go`'s `validatePeerCert`, which does correctly derive the trusted VPN address set purely from the verified certificate's `Networks()` — never from a peer-suppliable message field: [5](#0-4) 

`handleCreateRelayRequest` breaks that pattern: it lets an authenticated peer with a valid certificate for its *own* address `A` claim, via `RelayFromAddr`, to be relaying traffic "from" any other address `B` (including another legitimate host's VPN address) without proving control of `B`'s certificate. The receiving node then binds relay routing state (`AddRelay`, `relayState.InsertRelay`) to that attacker-chosen `from` address over the connection actually authenticated as `A`.

## Impact Explanation
This allows an authenticated-but-malicious mesh member to poison relay routing state by impersonating another host's VPN address in relay negotiation:
- It can register itself (via a cooperating or compromised relay node) as the "terminal" relay endpoint for an arbitrary victim VPN address `B` that it does not own a certificate for, since `TerminalType`/`Established` relay state is created keyed on the spoofed `from` rather than the verified `h.vpnAddrs`.
- Combined with `SendVia`/`handleOutsideRelayPacket` forwarding logic that trusts `relayState` mappings, this can redirect traffic intended for `B` toward the attacker's tunnel, or cause the relay/target host to build inconsistent/duplicated relay state for `B`, which is a form of remote state poisoning distinct from any cryptographic bypass but achieved purely by mismatched validation of an attacker-supplied identity field against the authenticated peer identity.

## Likelihood Explanation
Any node holding a valid Nebula certificate signed by the trusted CA (i.e., any legitimate mesh member, not necessarily malicious by cert but potentially compromised or rogue) can trigger this by sending a `CreateRelayRequest` control message with a forged `RelayFromAddr`/`OldRelayFromAddr` differing from its own certificate-assigned network. No lighthouse impersonation, certificate forgery, or handshake bypass is required — only participation as an authenticated relay/target, making this readily reachable in any deployment that uses relays (`relay.am_relay` / `relay.use_relays`).

## Recommendation
In `relayManager.handleCreateRelayRequest` (and symmetrically in `handleCreateRelayResponse`), validate that `from` (`m.RelayFromAddr`/`m.OldRelayFromAddr`) is contained in `h.vpnAddrs` (the certificate-verified addresses of the `HostInfo` that delivered the control message) before using it to create or update any relay state. Reject the message otherwise, mirroring the identity checks already performed in `validatePeerCert`.

## Proof of Concept
1. Node `A` (valid cert for VPN address `10.0.0.5`) establishes a normal encrypted tunnel to relay node `R` (`am_relay=true`).
2. `A` sends `header.Control` / `NebulaControl_CreateRelayRequest` to `R` with `RelayFromAddr = 10.0.0.9` (belonging to victim node `B`, which `A` does not hold a certificate for) and `RelayToAddr` set to some third host `T` reachable via `R`.
3. In `handleCreateRelayRequest`, `from = 10.0.0.9` is accepted with no check against `h.vpnAddrs` (`A`'s real address); `R` calls `AddRelay(..., from=10.0.0.9, ...)` and registers `A`'s tunnel as the relay path for `10.0.0.9`.
4. `R` now believes traffic tagged with source `10.0.0.9` should be relayed via `A`'s tunnel, even though `A` never proved ownership of that address — demonstrating relay-state poisoning via an unchecked, peer-supplied address field.

### Citations

**File:** outside.go (L168-169)
```go
	case header.Control:
		f.relayManager.HandleControlMsg(hostinfo, out, f)
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

**File:** handshake_manager.go (L992-1028)
```go
// validatePeerCert checks the peer certificate for self-connection and remote allow list.
// Returns the VPN addrs, whether any of them fall within one of our own VPN
// networks, and true if valid; false if rejected.
func (hm *HandshakeManager) validatePeerCert(via ViaSender, remoteCert *cert.CachedCertificate) ([]netip.Addr, bool, bool) {
	f := hm.f
	vpnNetworks := remoteCert.Certificate.Networks()

	// The cert package rejects host certs with no networks at parse time, so
	// reaching this state would mean an invariant was bypassed elsewhere.
	// Refuse explicitly so downstream code (which indexes vpnAddrs[0]) can't
	// panic if that invariant ever changes.
	if len(vpnNetworks) == 0 {
		f.l.Info("No networks in certificate",
			"from", via, "cert", remoteCert)
		return nil, false, false
	}

	vpnAddrs := make([]netip.Addr, len(vpnNetworks))
	anyVpnAddrsInCommon := false

	for i, network := range vpnNetworks {
		if f.myVpnAddrsTable.Contains(network.Addr()) {
			f.l.Error("Refusing to handshake with myself",
				"vpnNetworks", vpnNetworks,
				"from", via,
				"certName", remoteCert.Certificate.Name(),
				"certVersion", remoteCert.Certificate.Version(),
				"fingerprint", remoteCert.Fingerprint,
				"issuer", remoteCert.Certificate.Issuer(),
			)
			return nil, false, false
		}
		vpnAddrs[i] = network.Addr()
		if f.myVpnNetworksTable.Contains(network.Addr()) {
			anyVpnAddrsInCommon = true
		}
	}
```
