Confirmed: `hostinfo` in `outside.go:168-169` is resolved via `f.hostMap.QueryIndex(h.RemoteIndex)` and the packet is decrypted (`hostinfo.ConnectionState.Decrypt`) before `HandleControlMsg` is called, so `hostinfo` (`h` in `relay_manager.go`) is a cryptographically authenticated peer — its `vpnAddrs` were assigned by the peer's Nebula certificate during the handshake. However, `relayManager.handleCreateRelayRequest` never checks that the `RelayFromAddr` field asserted inside the (encrypted, but attacker-authored) control message actually equals `h.vpnAddrs[0]`.

### Title
Broken access control on relay identity — `RelayFromAddr` is trusted without binding to the authenticated peer's certificate address - (File: `relay_manager.go`)

### Summary
`relayManager.handleCreateRelayRequest` derives the relayed identity (`from`) solely from the attacker-controlled `NebulaControl.RelayFromAddr`/`OldRelayFromAddr` field of the message payload, instead of validating it against the certificate-bound `vpnAddrs` of the authenticated sender `h`. This is the direct analog of the reported "Broken Access Control on Organization ID" bug: an authenticated party can substitute an identifier belonging to someone else (there: `orgId`; here: `RelayFromAddr`) and have the system act on their behalf without ownership verification.

### Finding Description
`relay_manager.go` in `handleCreateRelayRequest` does: [1](#0-0) 
`from` and `target` are unmarshalled straight from the message body (`m.RelayFromAddr`/`m.RelayToAddr`), which is fully attacker-controlled content inside an authenticated-but-otherwise-arbitrary Control packet. The only self-referential checks performed are `f.myVpnAddrsTable.Contains(from)` (rejects only if the attacker claims to be *me*) and `f.myVpnAddrsTable.Contains(target)`. There is no check that `from == h.vpnAddrs[0]`, i.e. that the claimed "relay from" identity is the identity the certificate actually authenticated for hostinfo `h`.

As a result, relay state keyed by `from` gets attached to `h` (the actual, authenticated peer) via: [2](#0-1) 
and `h.relayState.QueryRelayForByIp(from)` / `AddRelay(rm.l, h, f.hostMap, from, ...)` install/lookup relay routing state under the spoofed `from` address rather than `h`'s own authenticated vpnAddr. This routing state subsequently governs which `HostInfo` traffic addressed to/from `from` gets forwarded through in `outside.go`'s `handleOutsideRelayPacket` and `inside.go`/`SendVia`'s relay path lookups (`f.hostMap.QueryVpnAddrsRelayFor`), meaning a malicious authenticated peer can insert itself into another host's relay topology by simply asserting that host's vpn address as `RelayFromAddr`, without ever presenting that host's certificate. [3](#0-2) [4](#0-3) 

Contrast this with the CA/firewall path, where every identity check is consistently anchored to `c.Certificate`/`h.vpnAddrs` derived from `CAPool.VerifyCertificate` [5](#0-4)  and firewall rule matching uses `c.Certificate.Name()`/`c.InvertedGroups`/`caPool.GetCAForCert` rather than attacker-supplied identifiers [6](#0-5) . The relay-request path breaks this pattern by trusting a self-declared address field instead of the authenticated hostinfo identity — the same class of flaw as the reported vulnerability (trusting a caller-supplied identifier instead of verifying it belongs to the caller).

### Impact Explanation
An authenticated Nebula node (any node holding a valid, signed certificate for the network — not a CA holder, just an ordinary peer) can cause a relay node to install relay-routing state associating itself with an arbitrary victim's vpn address. Depending on how this state is later consulted for forwarding decisions, this can enable traffic misdirection/interception for relayed connections to that victim address, and can corrupt relay bookkeeping (`RelayState.relayForByAddr`) with entries the attacker doesn't own, which is a remote state poisoning primitive within the relay subsystem. It matches the report's core theme — obtaining/asserting another principal's identifier and having the system act on it without an ownership check — mapped onto Nebula's relay identity binding instead of a web `orgId`.

### Likelihood Explanation
Likelihood is high among nodes that use relays (`relay.use_relays`/`relay.am_relay`): any peer that can complete a normal handshake (i.e., holds a certificate signed by the network's CA, exactly like any other legitimate node) can send a `Control`/`CreateRelayRequest` message with a forged `RelayFromAddr`, since no additional privilege beyond a valid handshake is required to reach `handleCreateRelayRequest`.

### Recommendation
In `handleCreateRelayRequest` (and symmetrically in `handleCreateRelayResponse`/`EstablishRelay`), verify that `from` (`RelayFromAddr`/`OldRelayFromAddr`) equals one of `h.vpnAddrs` — the certificate-verified identity of the actual sender `h` — before using it as the key for `AddRelay`/`QueryRelayForByIp`/relay state installation. Reject and drop the control message if the asserted address is not one the peer's certificate actually authenticates.

### Proof of Concept
1. Node `A` (attacker) completes a normal Nebula handshake with relay node `R` (`am_relay: true`), presenting *A's own* valid certificate/cert-bound vpnAddr `Va`.
2. `A` sends `R` a `header.Control` / `NebulaControl_CreateRelayRequest` message with `RelayFromAddr` set to `Vv` (victim `V`'s vpn address, not `A`'s), and `RelayToAddr` set to some target `T` that `R` can relay to.
3. In `relay_manager.go`, `handleCreateRelayRequest` computes `from = Vv` from the message body; since `f.myVpnAddrsTable.Contains(Vv)` is false, execution proceeds to install/query relay state via `h.relayState.QueryRelayForByIp(from)` / `AddRelay(rm.l, h, f.hostMap, from, ...)` on `h` (which is `A`'s authenticated hostinfo), even though `A` never proved ownership of `Vv`.
4. `R` now has relay-routing state tying vpn address `Vv` to `A`'s tunnel, without `V`'s certificate ever having been presented, demonstrating the missing ownership/authorization check analogous to the reported `orgId` broken access control.

### Citations

**File:** relay_manager.go (L426-447)
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
```

**File:** relay_manager.go (L481-493)
```go
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

**File:** outside.go (L176-217)
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

**File:** hostmap.go (L583-614)
```go
func (hm *HostMap) QueryVpnAddrsRelayFor(targetIps []netip.Addr, relayHostIp netip.Addr) (*HostInfo, *Relay, error) {
	hm.RLock()
	defer hm.RUnlock()

	// This runs per relayed packet, so check the primary with a single map probe and only consult
	// moreHosts when the primary can't relay for us.
	h, ok := hm.Hosts[relayHostIp]
	if !ok {
		return nil, nil, errors.New("unable to find host")
	}

	for _, targetIp := range targetIps {
		r, ok := h.relayState.QueryRelayForByIp(targetIp)
		if ok && r.State == Established {
			return h, r, nil
		}
	}

	if list, ok := hm.moreHosts[relayHostIp]; ok {
		// list[0] is the primary we already checked
		for _, h := range list[1:] {
			for _, targetIp := range targetIps {
				r, ok := h.relayState.QueryRelayForByIp(targetIp)
				if ok && r.State == Established {
					return h, r, nil
				}
			}
		}
	}

	return nil, nil, errors.New("unable to find host with relay")
}
```

**File:** cert/ca_pool.go (L154-196)
```go
// VerifyCertificate verifies the certificate is valid and is signed by a trusted CA in the pool.
// If the certificate is valid then the returned CachedCertificate can be used in subsequent verification attempts
// to increase performance.
func (ncp *CAPool) VerifyCertificate(now time.Time, c Certificate) (*CachedCertificate, error) {
	if c == nil {
		return nil, fmt.Errorf("no certificate")
	}
	fp, err := c.Fingerprint()
	if err != nil {
		return nil, fmt.Errorf("could not calculate fingerprint to verify: %w", err)
	}

	signer, err := ncp.verify(c, now, fp, "")
	if err != nil {
		return nil, err
	}

	// Pre nebula v1.10.3 could generate signatures in either high or low s form and validation
	// of signatures allowed for either. Nebula v1.10.3 and beyond clamps signature generation to low-s form
	// but validation still allows for either. Since a change in the signature bytes affects the fingerprint, we
	// need to test both forms until such a time comes that we enforce low-s form on signature validation.
	fp2, err := CalculateAlternateFingerprint(c)
	if err != nil {
		return nil, fmt.Errorf("could not calculate alternate fingerprint to verify: %w", err)
	}
	if fp2 != "" && ncp.IsBlocklisted(fp2) {
		return nil, ErrBlockListed
	}

	cc := CachedCertificate{
		Certificate:       c,
		InvertedGroups:    make(map[string]struct{}),
		Fingerprint:       fp,
		fingerprint2:      fp2,
		signerFingerprint: signer.Fingerprint,
	}

	for _, g := range c.Groups() {
		cc.InvertedGroups[g] = struct{}{}
	}

	return &cc, nil
}
```

**File:** firewall.go (L746-767)
```go
func (fc *FirewallCA) match(p firewall.Packet, c *cert.CachedCertificate, caPool *cert.CAPool) bool {
	if fc == nil {
		return false
	}

	if fc.Any.match(p, c) {
		return true
	}

	if t, ok := fc.CAShas[c.Certificate.Issuer()]; ok {
		if t.match(p, c) {
			return true
		}
	}

	s, err := caPool.GetCAForCert(c.Certificate)
	if err != nil {
		return false
	}

	return fc.CANames[s.Certificate.Name()].match(p, c)
}
```
