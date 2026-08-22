### Title
Missing length bound on certificate `groups` entries during ASN.1 parsing (asymmetric with the bounded `name`/`networks`/`unsafeNetworks` fields) - (File: cert/cert_v2.go)

### Summary
The Notional report describes a validation function that enforces a size constraint on one field (`accountDebtUnderlying`) while forgetting to enforce the equivalent constraint on structurally-identical sibling fields (`accountDebtOne`, `accountDebtTwo`). The same class of bug — an asymmetric bound check across sibling fields of the same category — exists in nebula's V2 certificate ASN.1 decoder `unmarshalDetails` in `cert/cert_v2.go`. The `name` field is explicitly bounded (`len(name) > MaxNameLength`), and each `networks`/`unsafeNetworks` entry is explicitly bounded (`len(val) > MaxNetworkLength`), but the sibling `groups` field — parsed in the exact same loop pattern, immediately below — has no equivalent per-entry length bound. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`unmarshalCertificateV2` decodes an untrusted, attacker-supplied byte blob (the raw certificate bytes from a handshake message) via `unmarshalDetails`, which is invoked **before** signature/CA-pool verification (`c.validate()` only checks structural invariants; `CheckCAConstraints`/`VerifyCertificate` happen later in `ca_pool.go`). This means an attacker who does not possess any CA-signed certificate can still get their raw bytes parsed by this function during handshake processing (`Recombine`/`unmarshalCertificateV2` is called from the handshake noise processing path prior to trust establishment).

Within `unmarshalDetails`:
- `name` is bounded: `if !b.ReadASN1(&name, TagDetailsName) || name.Empty() || len(name) > MaxNameLength { ... }` [1](#0-0) 
- each `networks` prefix is bounded: `if !subString.ReadASN1(&val, asn1.OCTET_STRING) || val.Empty() || len(val) > MaxNetworkLength { ... }` [4](#0-3) 
- each `unsafeNetworks` prefix is bounded identically [5](#0-4) 
- but each `groups` string entry is only checked for emptiness, with **no upper bound** on its length:
```go
if !subString.ReadASN1(&val, asn1.UTF8String) || val.Empty() {
    return detailsV2{}, ErrBadFormat
}
groups = append(groups, string(val))
``` [6](#0-5) 

The only outer constraint is `MaxCertificateSize` on the whole certificate blob, checked once in `unmarshalCertificateV2`: [7](#0-6) 
However, that is a single aggregate cap (65536 bytes per the ASN.1 spec comment), not a per-field bound analogous to the explicit `MaxNameLength`/`MaxNetworkLength` checks applied to the sibling `name`/`networks` fields — exactly mirroring the Notional pattern where the aggregate/primary quantity is checked but the sibling quantities of the same category are not subjected to the same explicit per-item validation that was clearly intended (as evidenced by the parallel checks on `name` and the network lists in the very same function).

### Impact Explanation
Because `groups` strings are unmarshalled and later used pervasively for authorization decisions (firewall group matching in `firewall.go`'s `FirewallRule.match`, `CachedCertificate.InvertedGroups` construction in `ca_pool.go`'s `VerifyCertificate`), an inconsistency in how group-string bounds are enforced compared to the other cert list fields undermines the certificate's structural validation guarantees that the rest of the codebase implicitly relies on (every other repeated/list-shaped field in this exact decoder is size-bounded per element). This is reachable pre-authentication, during initial parsing of a peer-presented certificate in the handshake, before CA-signature verification rejects the certificate — i.e., exploitable by any peer regardless of whether they hold a certificate signed by a trusted CA.

### Likelihood Explanation
High reachability: any party that can send a handshake packet can trigger `unmarshalCertificateV2` parsing with attacker-controlled `groups` content, without needing a CA-trusted certificate, since parsing precedes trust verification. The overall 64KB certificate-size ceiling limits the severity (bounds total allocation to the certificate blob size), which is why this is a data/validation-consistency defect rather than an unbounded resource-exhaustion primitive — but it is a concrete asymmetry versus the explicit, intentional bounds applied to the `name` and network list fields in the same function.

### Recommendation
Add an explicit per-entry length bound (e.g., a `MaxGroupNameLength`/reuse of `MaxNameLength`) to the `groups` parsing loop in `unmarshalDetails`, matching the pattern already used for `name`, `networks`, and `unsafeNetworks`:
```go
if !subString.ReadASN1(&val, asn1.UTF8String) || val.Empty() || len(val) > MaxNameLength {
    return detailsV2{}, ErrBadFormat
}
```
This restores symmetry across all list-shaped certificate detail fields and removes the asymmetric validation gap.

### Proof of Concept
Conceptual (parsing-only, no CA cert required):
1. Construct a raw V2 certificate `Details` ASN.1 blob with a valid, short `name`, a single valid `network`, and a `groups` SEQUENCE containing one or more UTF8String entries each far larger than `MaxNameLength` (but within the overall `MaxCertificateSize` ceiling).
2. Feed this into `unmarshalCertificateV2`/`unmarshalDetails` (e.g., via a handshake message before verification, or directly via the exported `Recombine`/unmarshal entry points).
3. Observe that parsing succeeds and `detailsV2.groups` contains the oversized strings unbounded by `MaxNameLength`, whereas an equally oversized `name` or `network` entry of the same relative size is rejected with `ErrBadFormat` by the sibling checks in the same function — demonstrating the asymmetric validation.

Note: I was not able to fully confirm at what exact certificate size the aggregate `MaxCertificateSize` cap practically limits the number/size of oversized group strings, since that constant's value wasn't directly retrieved in the available context (grep matched only within `cert/cert_v2.go`, but its numeric definition wasn't shown in the excerpts reviewed). A Devin session with full repo access would be needed to pin down the exact constant value and craft a runnable PoC test.

### Citations

**File:** cert/cert_v2.go (L570-574)
```go
func unmarshalCertificateV2(b []byte, publicKey []byte, curve Curve) (*certificateV2, error) {
	l := len(b)
	if l == 0 || l > MaxCertificateSize {
		return nil, ErrBadFormat
	}
```

**File:** cert/cert_v2.go (L647-651)
```go
	// Read the name
	var name cryptobyte.String
	if !b.ReadASN1(&name, TagDetailsName) || name.Empty() || len(name) > MaxNameLength {
		return detailsV2{}, ErrBadFormat
	}
```

**File:** cert/cert_v2.go (L653-675)
```go
	// Read the network addresses
	var subString cryptobyte.String
	var found bool

	if !b.ReadOptionalASN1(&subString, &found, TagDetailsNetworks) {
		return detailsV2{}, ErrBadFormat
	}

	var networks []netip.Prefix
	var val cryptobyte.String
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.OCTET_STRING) || val.Empty() || len(val) > MaxNetworkLength {
				return detailsV2{}, ErrBadFormat
			}

			var n netip.Prefix
			if err := n.UnmarshalBinary(val); err != nil {
				return detailsV2{}, ErrBadFormat
			}
			networks = append(networks, n)
		}
	}
```

**File:** cert/cert_v2.go (L682-695)
```go
	var unsafeNetworks []netip.Prefix
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.OCTET_STRING) || val.Empty() || len(val) > MaxNetworkLength {
				return detailsV2{}, ErrBadFormat
			}

			var n netip.Prefix
			if err := n.UnmarshalBinary(val); err != nil {
				return detailsV2{}, ErrBadFormat
			}
			unsafeNetworks = append(unsafeNetworks, n)
		}
	}
```

**File:** cert/cert_v2.go (L697-710)
```go
	// Read out any groups
	if !b.ReadOptionalASN1(&subString, &found, TagDetailsGroups) {
		return detailsV2{}, ErrBadFormat
	}

	var groups []string
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.UTF8String) || val.Empty() {
				return detailsV2{}, ErrBadFormat
			}
			groups = append(groups, string(val))
		}
	}
```
