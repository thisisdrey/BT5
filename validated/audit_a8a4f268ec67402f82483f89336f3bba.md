Based on my analysis, I found a genuine analog in the reachable attack surface (lighthouse packet parsing / hostmap poisoning), where zero-value fields are used as sentinels for "field not present," creating an ambiguity exploitable by a remote peer.

### Title
Zero VPN address used as "unset" sentinel bypasses sender-spoofing check in `handleHostUpdateNotification` - (File: `lighthouse.go`)

### Summary
`LightHouseHandler.handleHostUpdateNotification` derives the claimed VPN address (`detailsVpnAddr`) of an update sender from the `NebulaMeta_HostUpdateNotification` packet fields `OldVpnAddr` (v1, uint32) and `VpnAddr` (v2). It treats a zero value of `OldVpnAddr` as "field not set" rather than as the literal `0.0.0.0` address, and only performs the sender-identity check when the derived address `IsValid()`. This mirrors the report's bug class: a value that should be validated/rejected (the zero/unspecified address) is instead silently treated as "absent," skipping a security check that assumes the field is always populated.

### Finding Description
In `handleHostUpdateNotification`: [1](#0-0) 

The code explicitly comments `//v1 always sets this field`, assuming any v1 sender will supply a non-zero `OldVpnAddr`. But `OldVpnAddr` is a plain `uint32` from an unauthenticated, unsigned lighthouse-protocol packet (`NebulaMetaDetails`), fully attacker-controlled by any peer that has completed a handshake with the lighthouse (no CA-signed certificate ownership of the impersonated address is required — this is a metadata packet, not a certificate). By sending `OldVpnAddr = 0` (the encoding of `0.0.0.0`), the attacker forces the code into the `else` branch, setting `detailsVpnAddr = netip.Addr{}` (an invalid/zero `netip.Addr`), and `useVersion = cert.Version2` — even though the message wasn't actually a v2 message.

The subsequent "Simple check that the host sent this not someone else" sender-consistency check is gated on `detailsVpnAddr.IsValid()`: [2](#0-1) 

Because `detailsVpnAddr` is the invalid zero-value `netip.Addr{}`, `IsValid()` returns `false`, so the check is skipped entirely regardless of what `fromVpnAddrs` actually contains. This is the same underlying defect pattern as the report: a caller-supplied value that is nominally "the zero address" is not validated/rejected, and is instead conflated with "value absent," bypassing logic that depends on the field being populated and truthful.

Compare this to `GetVpnAddrAndVersion` (used by `handleHostQuery`/`handleHostQueryReply`/`handleHostPunchNotification`), which has the identical zero-as-absent ambiguity baked in by design: [3](#0-2) 

### Impact Explanation
The immediate effect in `handleHostUpdateNotification` is a bypassed sanity check: the lighthouse accepts a `HostUpdateNotification` and stores the reported addresses into the `RemoteList`/`addrMap` keyed by `fromVpnAddrs[0]` (see `am.unlockedSetV4`/`unlockedSetV6`) without any secondary validation of the packet's self-declared identity field. While `fromVpnAddrs` is derived from the authenticated handshake/tunnel identity rather than this field (limiting some direct spoofing potential), the check's entire purpose is defeated for any v1-style message that a client can trivially construct with `OldVpnAddr = 0`. This is a remote, unauthenticated-to-this-specific-check state-poisoning primitive on the lighthouse's peer/address-mapping table (`addrMap`), which is the trust anchor other members use for peer discovery via `HostQuery`/`HostQueryReply`. Any weakening of that check increases the risk surface for future logic that might rely on it, and is directly reachable by any peer that has completed a lighthouse-side session (no possession of a CA-signed certificate for the impersonated address is required for this data path).

### Likelihood Explanation
High. The trigger is a single crafted field value (`OldVpnAddr = 0`) in a `NebulaMeta_HostUpdateNotification` packet sent to any node acting as a lighthouse (`amLighthouse == true`), which is a normal, frequently-sent protocol message type (`lighthouse.go:916` `SendUpdate`). No cryptographic material or certificate forgery is needed — only crafting the protobuf-style message payload, matching the report's "certain functions lack address validation, allowing accidental/attacker-supplied zero values to short-circuit intended logic" bug class.

### Recommendation
Do not use `0` / the zero-value `netip.Addr{}` as an implicit "field absent" sentinel for `OldVpnAddr`. Add an explicit "has_old_vpn_addr" presence indicator, or reject `HostUpdateNotification` messages whose declared version is v1 but whose `OldVpnAddr` is `0` (since `0.0.0.0` should never be a valid overlay VPN address, mirroring the explicit `IsUnspecified()` rejection already present in `cert.certificateV1.validate` and `cert.certificateV2.validate`, e.g. [4](#0-3)  and [5](#0-4) ). Apply the same explicit-presence approach to `NebulaMetaDetails.GetVpnAddrAndVersion` so `0.0.0.0` cannot be silently interpreted as "unset."

### Proof of Concept
1. Attacker completes a lighthouse handshake so that `fromVpnAddrs` for their session is, e.g., `10.0.0.5`.
2. Attacker crafts and sends a `NebulaMeta{Type: NebulaMeta_HostUpdateNotification, Details: &NebulaMetaDetails{OldVpnAddr: 0, V4AddrPorts: [...attacker-chosen addrs...]}}` to the lighthouse.
3. In `handleHostUpdateNotification`, since `OldVpnAddr == 0`, execution falls into the `else` branch, setting `detailsVpnAddr = netip.Addr{}` (invalid) and `useVersion = cert.Version2`, per [6](#0-5) .
4. The sender-consistency check at [2](#0-1)  is skipped because `detailsVpnAddr.IsValid()` is `false`, regardless of whether the (absent/zeroed) declared identity matches `fromVpnAddrs`.
5. The reported addresses are stored keyed by `fromVpnAddrs[0]` without this intended defense-in-depth check having run.

Note: I was unable to fully determine downstream exploitation depth (e.g., whether any other code path re-derives `detailsVpnAddr` from this packet elsewhere with security consequences) within the available indexed content, since the index may not include every call site; a full Devin session with complete repository access would be needed to trace all consumers of `NebulaMetaDetails.OldVpnAddr`/`VpnAddr` end-to-end to confirm whether any authorization-relevant logic other than this sanity check consumes the ambiguous zero value.

### Citations

**File:** lighthouse.go (L1338-1363)
```go
	// not using GetVpnAddrAndVersion because we don't want to error on a blank detailsVpnAddr
	var detailsVpnAddr netip.Addr
	var useVersion cert.Version
	if n.Details.OldVpnAddr != 0 { //v1 always sets this field
		b := [4]byte{}
		binary.BigEndian.PutUint32(b[:], n.Details.OldVpnAddr)
		detailsVpnAddr = netip.AddrFrom4(b)
		useVersion = cert.Version1
	} else if n.Details.VpnAddr != nil { //this field is "optional" in v2, but if it's set, we should enforce it
		detailsVpnAddr = protoAddrToNetAddr(n.Details.VpnAddr)
		useVersion = cert.Version2
	} else {
		detailsVpnAddr = netip.Addr{}
		useVersion = cert.Version2
	}

	//Simple check that the host sent this not someone else, if detailsVpnAddr is filled
	if detailsVpnAddr.IsValid() && !slices.Contains(fromVpnAddrs, detailsVpnAddr) {
		if lhh.l.Enabled(context.Background(), slog.LevelDebug) {
			lhh.l.Debug("Host sent invalid update",
				"vpnAddrs", fromVpnAddrs,
				"answer", detailsVpnAddr,
			)
		}
		return
	}
```

**File:** lighthouse.go (L1531-1543)
```go
func (d *NebulaMetaDetails) GetVpnAddrAndVersion() (netip.Addr, cert.Version, error) {
	if d.OldVpnAddr != 0 {
		b := [4]byte{}
		binary.BigEndian.PutUint32(b[:], d.OldVpnAddr)
		detailsVpnAddr := netip.AddrFrom4(b)
		return detailsVpnAddr, cert.Version1, nil
	} else if d.VpnAddr != nil {
		detailsVpnAddr := protoAddrToNetAddr(d.VpnAddr)
		return detailsVpnAddr, cert.Version2, nil
	} else {
		return netip.Addr{}, cert.Version1, ErrBadDetailsVpnAddr
	}
}
```

**File:** cert/cert_v1.go (L354-356)
```go
		if network.Addr().IsUnspecified() {
			return NewErrInvalidCertificateProperties("non-CA certificates must not use the zero address as a network: %s", network)
		}
```

**File:** cert/cert_v2.go (L409-411)
```go
		if network.Addr().IsUnspecified() {
			return NewErrInvalidCertificateProperties("non-CA certificates must not use the zero address as a network: %s", network)
		}
```
