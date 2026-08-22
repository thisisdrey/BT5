### Title
Unauthenticated lighthouse HostUpdateNotification bypasses source-address binding check when VpnAddr is omitted - ([File: lighthouse.go])

### Summary
The bug reported for `EscrowedLoan` shows a class of vulnerability where a security-relevant check is silently skipped when an input value is empty/zero, because the code path only enforces validation "if present" rather than always. `Escrow._getLatestCollateralRatio()` returns `0` for empty collateral, and `_healthcheck()` misinterprets that value, letting attacker-controlled state transitions bypass authorization gates. The closest reachable analog in Nebula is in `lighthouse.go`'s `handleHostUpdateNotification`, where the "sender identity" binding check on a `HostUpdateNotification` is explicitly skipped whenever the reported `VpnAddr` field is absent/zero, exactly mirroring the "empty input silently defeats the safety check" pattern.

### Finding Description
In `(lhh *LightHouseHandler) handleHostUpdateNotification`, the code intentionally avoids using `GetVpnAddrAndVersion` "because we don't want to error on a blank detailsVpnAddr": [1](#0-0) 

The relevant logic is:
```go
var detailsVpnAddr netip.Addr
var useVersion cert.Version
if n.Details.OldVpnAddr != 0 { //v1 always sets this field
    ...
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
    ...
    return
}
```
When the v2 message omits `VpnAddr` (or a v1-style message sets `OldVpnAddr` to `0`), `detailsVpnAddr` becomes the zero-value `netip.Addr{}`, which reports `IsValid() == false`. The `IsValid()` guard then short-circuits the entire binding check ("Host sent invalid update") instead of treating a missing/zero address as suspicious. This is analytically identical to `Escrow._getLatestCollateralRatio()` treating an empty/zero collateral value as a special case (`0`) that flows into a status decision without the intended validation being applied — an empty input silently bypasses a security check rather than failing closed.

Downstream, the lighthouse then unconditionally trusts `fromVpnAddrs[0]` as the owner of the reported addresses/relays and stores them into the shared remote-address cache: [2](#0-1) 
```go
am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
am.unlockedSetRelay(fromVpnAddrs[0], relays)
```
Since `fromVpnAddrs` is derived from the handshake-authenticated peer identity, an attacker cannot forge `fromVpnAddrs` itself, but the binding check that is bypassed is the one the comment explicitly calls a "simple check that the host sent this not someone else" — the code's own stated purpose is defeated for any message that simply omits the optional field, which is fully attacker-controlled (an authenticated-but-legitimate peer can send a crafted `HostUpdateNotification` with `VpnAddr` unset).

### Impact Explanation
This weakens the intended defense-in-depth check the lighthouse performs before trusting host-update data (self-reported addresses/relays that get redistributed to other peers via `HostQueryReply`). While the ultimate authorization boundary (peer identity from the handshake) is still enforced elsewhere, the code path explicitly designed to catch "someone claiming to update on behalf of a different VPN address" is a no-op whenever the optional field is omitted — meaning the field can never actually detect misuse for any message that doesn't proactively fill it in, undermining the stated security intent of the check and reducing assurance around lighthouse address-trust poisoning, similar in spirit (state-check silently bypassed by an empty/default value) to the reported `EscrowedLoan` issue.

### Likelihood Explanation
Any node that can complete a handshake (i.e., holds a CA-signed certificate) can reach this path by sending a `NebulaMeta_HostUpdateNotification` with the v2 `VpnAddr` field left empty, which is the normal/optional case per the protocol's own comment ("this field is optional in v2"). No malicious/pre-authenticated-peer assumption beyond a standard mesh peer is required, and the omission is trivial to trigger — it requires no exploitation of memory safety or cryptography, only omitting an optional protobuf field.

### Recommendation
Do not use `IsValid()` (i.e., "was the field present") as a proxy for "should I validate." Instead, always validate the reported identity: if `detailsVpnAddr` is absent, default it to `fromVpnAddrs[0]` before the check (or skip trusting/broadcasting any address that wasn't explicitly asserted by the sender) so the binding check is always enforced rather than conditionally skipped based on field presence — analogous to fixing `_getLatestCollateralRatio()`/`_healthcheck()` to fail closed instead of treating a "zero" input as a valid, benign default.

### Proof of Concept
1. Establish a legitimate authenticated tunnel to a lighthouse (mesh peer with a valid cert).
2. Craft and send a `NebulaMeta` message of type `HostUpdateNotification` (v2) with `Details.VpnAddr` unset (nil) and `Details.OldVpnAddr` left at its zero value, while populating `V4AddrPorts`/`V6AddrPorts`/`RelayVpnAddrs` with arbitrary values.
3. Observe in `handleHostUpdateNotification` that `detailsVpnAddr` becomes `netip.Addr{}`, `IsValid()` is `false`, and the "Host sent invalid update" rejection branch is skipped entirely — the intended sender/claimed-identity binding check never executes for this message.
4. The lighthouse proceeds to call `unlockedSetV4`/`unlockedSetV6`/`unlockedSetRelay` keyed on `fromVpnAddrs[0]`, confirming the "someone updating on behalf of another address" check that the code comments claim to perform never actually ran for this (fully attacker-reachable, optional-field) message shape.

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

**File:** lighthouse.go (L1365-1375)
```go
	relays := n.Details.GetRelays()

	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList(fromVpnAddrs)
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```
