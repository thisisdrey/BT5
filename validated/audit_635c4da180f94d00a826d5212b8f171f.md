### Title
Unauthenticated `RelayFromAddr` accepted in `CreateRelayRequest` establishes relay/hostmap state for an arbitrary VPN address without verifying it belongs to the requesting peer - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` treats the `RelayFromAddr` field of an inbound `CreateRelayRequest` control message as trusted input and immediately creates `Established` relay state keyed on that address, without ever checking that the requesting peer (`h`) has a verified handshake/identity relationship with that claimed address. This is structurally the same bug class as the PoolTogether `SwappableYieldSource.transferFunds()` issue: a cross-entity operation (moving/associating value or identity from one context to another) is performed without validating that the two sides actually correspond to the same trusted principal.

### Finding Description
When a node acting as a relay terminal receives a `NebulaControl_CreateRelayRequest`, it extracts `from := protoAddrToNetAddr(m.RelayFromAddr)` directly from the wire message and, if no existing relay entry is found, calls: [1](#0-0) 

`AddRelay(rm.l, h, f.hostMap, from, &m.InitiatorRelayIndex, TerminalType, Established)` inserts the relay directly in the `Established` state, mapped by the attacker-supplied `from` address, on the `HostInfo` (`h`) of whoever sent the control message: [2](#0-1) 

The only identity checks performed are that the *target* (`m.RelayToAddr`) is one of "my" own VPN addresses, and that `from` is not "my" own address: [3](#0-2) 

There is no verification tying `from` to any certificate that `h` has actually presented, nor to any independent handshake between `h` and the claimed `from` peer. Compare this to the certificate/CA path, where `CAPool.verify` explicitly checks curve, expiry, signature, and CA constraints before trusting a certificate's claims: [4](#0-3) 

No equivalent "does this claimed identity actually match a verified principal" check exists for the `RelayFromAddr`/`RelayToAddr` fields carried in `NebulaControl` messages, exactly mirroring how `SwappableYieldSource.transferFunds()` moved funds between yield sources without checking that `_yieldSource.depositToken() == currentYieldSource.depositToken()`.

### Impact Explanation
Any peer that has completed a legitimate handshake with a relay-capable node (`relay.am_relay = true`) can send a `CreateRelayRequest` claiming an arbitrary `RelayFromAddr` (e.g. another real peer's VPN address) as long as the claimed `RelayToAddr` matches the relay's own address. The relay immediately installs `Established` relay state under that address on the attacker's `HostInfo`. Subsequent traffic the relay forwards for that "from" address will be routed through the attacker's tunnel rather than the legitimate peer's, poisoning the relay's/hostmap's routing state (`hm.Relays`, `relayState`) for a VPN identity the attacker never proved ownership of. This can be leveraged to hijack or intercept traffic destined for the spoofed address, or to bypass address-based firewall/allow-list assumptions that rely on relay state correctly reflecting verified peer identity.

### Likelihood Explanation
Exploitation requires only that the attacker be a normal, certificate-holding peer capable of reaching a relay-enabled node and sending a `Control` message — no privileged position or malicious relay/lighthouse role is needed. The check that is missing (`from` must correspond to a verified peer identity, not just an attacker-supplied field) is a single, simple validation, making the likelihood of this pattern being reachable and triggerable straightforward once a handshake exists between the attacker and the relay node.

### Recommendation
Before calling `AddRelay(..., from, ..., TerminalType, Established)`, verify that `from` is backed by an independently authenticated relationship — e.g., require that the relay (`h`) already holds `Established`/verified relay state for `from` obtained through its own completed handshake with that peer, or otherwise cryptographically bind `RelayFromAddr` to a certificate-derived VPN address rather than trusting the raw protobuf field. This mirrors the fix applied upstream for `SwappableYieldSource`, which added an explicit equality check (`depositToken` there, verified peer identity here) before allowing the cross-entity state transfer.

### Proof of Concept
1. Attacker `A` completes a normal handshake with relay node `R` (`relay.am_relay=true`), obtaining a `HostInfo` `h` on `R` for `A`'s real address `addrA`.
2. `A` sends `R` a `NebulaControl_CreateRelayRequest` with `RelayFromAddr = addrV` (a victim `V`'s real VPN address, not `A`'s) and `RelayToAddr = R`'s own address.
3. In `handleCreateRelayRequest`, `f.myVpnAddrsTable.Contains(target)` is true (target is `R`), `h.relayState.QueryRelayForByIp(addrV)` finds nothing, so `AddRelay(rm.l, h, f.hostMap, addrV, &m.InitiatorRelayIndex, TerminalType, Established)` runs, registering `h` (A's HostInfo) as the `Established` relay-for `addrV` on `R`.
4. `R` now forwards any traffic destined for `addrV` through `A`'s tunnel instead of `V`'s legitimate one, without `A` ever having proven control of `addrV`. [5](#0-4)

### Citations

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

**File:** cert/ca_pool.go (L210-250)
```go
func (ncp *CAPool) verify(c Certificate, now time.Time, certFp string, signerFp string) (*CachedCertificate, error) {
	if ncp.IsBlocklisted(certFp) {
		return nil, ErrBlockListed
	}

	signer, err := ncp.GetCAForCert(c)
	if err != nil {
		return nil, err
	}

	if signer.Certificate.Curve() != c.Curve() {
		return nil, ErrCurveMismatch
	}

	if signer.Certificate.Expired(now) {
		return nil, ErrRootExpired
	}

	if c.Expired(now) {
		return nil, ErrExpired
	}

	// If we are checking a cached certificate then we can bail early here
	// Either the root is no longer trusted or everything is fine
	if len(signerFp) > 0 {
		if signerFp != signer.Fingerprint {
			return nil, ErrFingerprintMismatch
		}
		return signer, nil
	}
	if !c.CheckSignature(signer.Certificate.PublicKey()) {
		return nil, ErrSignatureMismatch
	}

	err = CheckCAConstraints(signer.Certificate, c)
	if err != nil {
		return nil, err
	}

	return signer, nil
}
```
