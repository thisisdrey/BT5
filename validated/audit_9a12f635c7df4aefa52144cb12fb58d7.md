## Analog Found: Malformed IPv4 network mask decodes to 0 bits, creating a hidden "match-all" (0.0.0.0/0) network/subnet in V1 certificates

### Title
Malformed Nebula v1 certificate IP mask silently decodes to a `/0` network, bypassing CA network-constraint checks and firewall/lighthouse scoping - (File: `cert/cert_v1.go`)

### Summary
In `unmarshalCertificateV1`, the 32-bit "mask" half of each `Ips`/`Subnets` pair is converted with `net.IPMask(int2ip(rawIp)).Size()`. Go's `net.IPMask.Size()` returns `(0, 0)` whenever the mask bytes are not a canonical, left-aligned run of ones (e.g. `0x00000000`, or any non-contiguous bit pattern). The unmarshal code ignores this failure signal (`ones, _ := ...`) and unconditionally builds `netip.PrefixFrom(ip, ones)`, i.e. `netip.PrefixFrom(ip, 0)` — a `/0` prefix that matches every address in the IPv4 space. [1](#0-0) 

This is structurally identical to the LSP6 bug: a length/size decode that is expected to fail loudly on malformed input instead silently degrades to `0`, and that `0` is then fed straight into a "does X match/contain Y" check (`mask=bytes32(0)` there vs. `Bits()==0` prefix here), turning a scoped permission into an unscoped one.

### Finding Description
Nebula certificates encode `networks` (assigned VPN addresses) and `unsafeNetworks` (subnet routes a host is allowed to advertise/handle) as address+mask uint32 pairs. When parsing:

```go
ones, _ := net.IPMask(int2ip(rawIp)).Size()
nc.details.networks[i/2] = netip.PrefixFrom(ip, ones)
``` [2](#0-1) 

the error/validity signal from `Size()` is discarded. Any mask value that is not a canonical CIDR mask (all-ones prefix followed by all-zeros, e.g. an attacker-chosen `0x00000000`, `0x0F0F0F0F`, etc.) collapses to `ones == 0`. The resulting `netip.Prefix` has `Bits() == 0`, i.e. it is `0.0.0.0/0` (or `::/0` semantics for `.Contains`), matching literally every IPv4 address.

This value subsequently flows into two consumer paths that assume `networks`/`unsafeNetworks` are honestly scoped:

1. **CA constraint enforcement** — `checkCAConstraints` iterates a subordinate cert's `networks`/`unsafeNetworks` and checks that each is contained within, and no broader than, the signing CA's own networks:
```go
if signingNetwork.Contains(certNetwork.Addr()) && signingNetwork.Bits() <= certNetwork.Bits() {
    found = true
}
``` [3](#0-2) 
A cert whose declared network decodes to `/0` will only pass this check if the signer itself has no networks restriction (`len(signingNetworks) == 0`), which is common for a permissive/root CA — meaning an operator who controls cert issuance (analogous to "userA" in the original PoC, who legitimately signs certs but embeds a corrupted mask) can mint a certificate that nominally shows a normal-looking `Ips`/`Subnets` entry but that actually decodes as unrestricted.

2. **Firewall / routing scoping** — `Certificate.Networks()` / `UnsafeNetworks()` values are used to build `HostInfo.networks` (`buildNetworks`) and are consulted by `Firewall.Drop` to decide whether a remote address is a legitimate "VPN" vs "unsafe" vs "peer" address for that host:
```go
nwType, ok := h.networks.Lookup(fp.RemoteAddr)
...
case NetworkTypeUnsafe:
    break // nothing special, one day this may have different FW rules
``` [4](#0-3) 
If a cert's unsafe-network entry silently decodes to `/0`, that host would be treated as authorized to source/route traffic for literally any IP address, defeating the entire purpose of the per-host subnet restriction that firewall rules and route scoping rely on.

Because the malformed value looks like an ordinary IP+mask pair (there is no `0x0000` marker as in the LSP6 case — it is an arbitrary non-canonical 32-bit mask), this is a genuinely disguisable backdoor: reviewing the certificate's printed CIDR (e.g. via `nebula-cert print`) would show a plausible-looking prefix unless the reviewer specifically re-derives the mask and notices it isn't a valid CIDR mask.

### Impact Explanation
- A malicious or compromised cert-issuer (someone with legitimate signing authority, e.g. operating `nebula-cert sign`, or a party who is handed a signing role by a less careful admin) can embed a corrupted mask that decodes to `/0` for an `unsafeNetworks` entry, granting that node authority to claim/route for any subnet once its certificate is accepted by the CA pool and trusted peers.
- Combined with `checkCAConstraints`, if the top-level CA has no explicit `-networks`/`-subnets` restriction configured (the common/default case), this bypass is not even blocked at CA-constraint-check time — it succeeds silently.
- This does not require a "malicious peer already holding a CA-signed valid certificate they aren't supposed to have" — it is exploitable by whoever *is* issuing certificates in the first place (a permitted-but-adversarial signer), which is analogous to the "userA sets up the network for userB" scenario in the original finding, and is squarely within the reachable-by-an-attacker-with-no-valid-cert class since it's the mechanism by which certs are minted/validated, not something requiring an already-trusted peer identity.

### Likelihood Explanation
Requires social-engineering / insider conditions similar to the original finding: someone must be trusted to generate or sign certificates (or a signing tool with a bug/backdoor) to embed a non-canonical mask. It's not remotely triggerable by an anonymous network attacker sending packets. This mirrors the "Medium, decreased for social engineering requirement" judgment on the original LSP6 finding. I could not fully verify whether `nebula-cert sign`'s CLI/parsing path itself rejects non-canonical masks before calling into `unmarshalCertificateV1` (verification would require checking `cmd/nebula-cert/sign.go` and `cert/sign.go` mask-construction code, which was not fully explored) — if the CLI always constructs masks via `net.CIDRMask`, then this bug is only reachable via a manually/programmatically crafted raw certificate rather than through the standard tool, similar to how the original LSP6 PoC required bypassing the normal permission-setting API.

### Recommendation
In `unmarshalCertificateV1`, check the second return value of `net.IPMask(...).Size()` and reject the certificate (return an error) if the mask is not canonical, instead of silently accepting `ones == 0`:
```go
ones, bits := net.IPMask(int2ip(rawIp)).Size()
if bits == 0 {
    return nil, fmt.Errorf("invalid network mask in certificate")
}
```
Apply the same fix to both the `Ips`/`networks` loop and the `Subnets`/`unsafeNetworks` loop. Additionally, consider validating in `checkCAConstraints` that a `certNetwork.Bits() == 0` entry is never silently treated as "more specific than or equal to" an unrestricted signer.

### Proof of Concept
Not independently executed (no filesystem/build access), but the code path is deterministic: construct a `RawNebulaCertificateDetails.Subnets` pair `[ip, 0x00000000]` (or any non-canonical 32-bit value like `0x55555555`), marshal it into a `RawNebulaCertificate`, and call `unmarshalCertificateV1`. The resulting `unsafeNetworks[0]` will be `netip.PrefixFrom(ip, 0)`, i.e. `<ip>/0`, which `.Contains()` returns `true` for any address of that family — confirmable by inspecting [5](#0-4)  and Go's documented behavior of `net.IPMask.Size()` returning `(0,0)` for non-canonical masks.

### Citations

**File:** cert/cert_v1.go (L456-473)
```go
	var ip netip.Addr
	for i, rawIp := range rc.Details.Ips {
		if i%2 == 0 {
			ip = int2addr(rawIp)
		} else {
			ones, _ := net.IPMask(int2ip(rawIp)).Size()
			nc.details.networks[i/2] = netip.PrefixFrom(ip, ones)
		}
	}

	for i, rawIp := range rc.Details.Subnets {
		if i%2 == 0 {
			ip = int2addr(rawIp)
		} else {
			ones, _ := net.IPMask(int2ip(rawIp)).Size()
			nc.details.unsafeNetworks[i/2] = netip.PrefixFrom(ip, ones)
		}
	}
```

**File:** cert/ca_pool.go (L308-324)
```go
	// If the signer has a limited set of ip ranges to issue from make sure the cert only contains a subset
	signingNetworks := signer.Networks()
	if len(signingNetworks) > 0 {
		for _, certNetwork := range networks {
			found := false
			for _, signingNetwork := range signingNetworks {
				if signingNetwork.Contains(certNetwork.Addr()) && signingNetwork.Bits() <= certNetwork.Bits() {
					found = true
					break
				}
			}

			if !found {
				return fmt.Errorf("certificate contained a network assignment outside the limitations of the signing ca: %s", certNetwork.String())
			}
		}
	}
```

**File:** firewall.go (L433-446)
```go
	} else {
		nwType, ok := h.networks.Lookup(fp.RemoteAddr)
		if !ok {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
		switch nwType {
		case NetworkTypeVPN:
			break // nothing special
		case NetworkTypeVPNPeer:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrPeerRejected // reject for now, one day this may have different FW rules
		case NetworkTypeUnsafe:
			break // nothing special, one day this may have different FW rules
```
