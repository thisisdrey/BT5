Found the analog: `RemoteList.badRemotes` in `remote_list.go` grows without any bound, and `unlockedIsBad` performs a linear scan over it that is invoked repeatedly (per-address, per-collect, per-handshake-attempt) — this is structurally identical to the `AuctionDemo.sol` bug class: an attacker-controllable append-only slice consumed in an O(n) scan on a hot path.

### Title
Unbounded growth of `RemoteList.badRemotes` causes O(n²) blowup and handshake-processing DoS - (`remote_list.go`)

### Summary
`RemoteList.BlockRemote` appends every unique bad `UdpAddr` to `r.badRemotes` and never caps or prunes the slice except on a successful handshake (`RefreshFromHandshake`) or explicit `ResetBlockedRemotes`. `unlockedIsBad` performs `slices.Contains(r.badRemotes, remote)`, a linear scan, and this function is called from `BlockRemote` itself and from every address in `unlockedCollect` (for every learned/reported v4/v6 address and every DNS-resolved address) whenever the cache is rebuilt.

### Finding Description
`BlockRemote` at [1](#0-0)  is reachable pre-authentication: it is invoked whenever a handshake response is validated to have come from the wrong vpn address (a `ViaSender` that is not relayed), which an attacker can trigger repeatedly by sending crafted UDP packets/handshake replies claiming to be from arbitrary vpn addresses for a given underlay address, without needing a valid certificate to cause the block to be recorded (the "wrong vpn ip" check happens before full authentication succeeds). Each call does a full scan via `unlockedIsBad` at [2](#0-1)  before appending, and there is no maximum size enforced on `r.badRemotes`, unlike the parallel `MaxRemotes` cap enforced on `c.reported` in `unlockedSetV4`/`unlockedSetV6`/`unlockedPrependV4`/`unlockedPrependV6` at [3](#0-2) .

Because `unlockedCollect` at [4](#0-3)  calls `unlockedIsBad` for every learned address, every reported v4/v6 address, and every DNS-resolved address on every rebuild (triggered by `Rebuild`, itself called from `Len`, `ForEach`, `CopyAddrs` — all on the handshake/packet-processing path), the cost of processing a `RemoteList` scales with `O(len(reported) * len(badRemotes))`. Since `badRemotes` grows unbounded and unboundedly, this is directly analogous to the `AuctionDemo.sol` finding: an ever-growing array consumed inside per-request loops with no shrink/cap mechanism, leading to worsening latency that approaches a DoS as the attacker keeps forcing new entries into `badRemotes`.

### Impact Explanation
An attacker who can cause repeated "wrong vpn ip" handshake replies for a host (by controlling or spoofing underlay source addresses associated with a target vpn address) can grow `badRemotes` indefinitely. Every subsequent `Rebuild`/`ForEach`/`Len`/`CopyAddrs` call on that `RemoteList` (used for actual traffic routing decisions and handshake retries) becomes progressively more expensive, since each candidate reported address triggers a full linear scan of the growing bad list while holding `r.Lock()`. This degrades handshake and packet-routing performance for the host in question, and because the lock is held during the scan, it can also serialize/delay other operations on that same `RemoteList`, escalating toward denial of service, matching the "permanent DoS due to non-shrinking array usage in an unbounded loop" bug class from the report.

### Likelihood Explanation
Reachable without holding a CA-signed certificate: the "wrong vpn ip" detection that feeds `BlockRemote` happens as part of handshake/packet processing before full mutual authentication is established, so an attacker can flood spoofed or mismatched-source handshake traffic toward a victim's known underlay/vpn address pairing to keep adding entries. No special privileges or valid cert are required to trigger the append path; only a valid-looking handshake/`ViaSender` structure with a mismatching vpn address is needed. This is comparable in reachability to the original finding's un-authenticated bidder loop.

### Recommendation
Cap `r.badRemotes` similarly to the existing `MaxRemotes` cap used on `reported` addresses (e.g., stop appending once at capacity, or evict oldest/duplicate entries), and/or replace the linear-scan `slices.Contains` check with a set (`map[netip.AddrPort]struct{}`) for O(1) lookups so the cost does not grow with the number of blocked remotes. Additionally, evaluate whether `BlockRemote` should require any pre-authentication signal validation before recording an entry, to reduce spoof-ability of the trigger.

### Proof of Concept
1. Attacker crafts and sends repeated handshake/packet traffic that causes `BlockRemote` to be invoked with distinct `bad.UdpAddr` values for a victim's `RemoteList` (via mismatched vpn-address handshake replies), e.g. varying source port/IP combinations.
2. Each call appends to `r.badRemotes` at [5](#0-4)  with no upper bound.
3. As the victim's tunnel continues normal operation, every `Rebuild`/`ForEach`/`Len` call triggers `unlockedCollect`, which calls `unlockedIsBad` (a linear `O(n)` scan of `badRemotes`) once per learned/reported/DNS address [6](#0-5) .
4. As `badRemotes` grows without bound, this per-rebuild cost grows linearly with the number of forged mismatches the attacker has sent, degrading handshake/packet-path performance and potentially causing a denial of service under the lock held in `Rebuild`.

### Citations

**File:** remote_list.go (L377-396)
```go
// BlockRemote locks and records the address as bad, it will be excluded from the deduplicated address list
func (r *RemoteList) BlockRemote(bad ViaSender) {
	if bad.IsRelayed {
		return
	}

	r.Lock()
	defer r.Unlock()

	// Check if we already blocked this addr
	if r.unlockedIsBad(bad.UdpAddr) {
		return
	}

	// We copy here because we are taking something else's memory and we can't trust everything
	r.badRemotes = append(r.badRemotes, bad.UdpAddr)

	// Mark the next interaction must recollect/dedupe
	r.shouldRebuild = true
}
```

**File:** remote_list.go (L442-445)
```go
// unlockedIsBad assumes you have the write lock and checks if the remote matches any entry in the blocked address list
func (r *RemoteList) unlockedIsBad(remote netip.AddrPort) bool {
	return slices.Contains(r.badRemotes, remote)
}
```

**File:** remote_list.go (L454-493)
```go
// unlockedSetV4 assumes you have the write lock and resets the reported list of ips for this owner to the list provided
// and marks the deduplicated address list as dirty
func (r *RemoteList) unlockedSetV4(ownerVpnIp, vpnIp netip.Addr, to []*V4AddrPort, check checkFuncV4) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeV4(ownerVpnIp)

	// Reset the slice
	c.reported = c.reported[:0]

	// We can't take their array but we can take their pointers
	for _, v := range to[:minInt(len(to), MaxRemotes)] {
		if check(vpnIp, v) {
			c.reported = append(c.reported, v)
		}
	}
}

func (r *RemoteList) unlockedSetRelay(ownerVpnIp netip.Addr, to []netip.Addr) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeRelay(ownerVpnIp)

	// Reset the slice
	c.relay = c.relay[:0]

	// We can't take their array but we can take their pointers
	c.relay = append(c.relay, to[:minInt(len(to), MaxRemotes)]...)
}

// unlockedPrependV4 assumes you have the write lock and prepends the address in the reported list for this owner
// This is only useful for establishing static hosts
func (r *RemoteList) unlockedPrependV4(ownerVpnIp netip.Addr, to *V4AddrPort) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeV4(ownerVpnIp)

	// We are doing the easy append because this is rarely called
	c.reported = append([]*V4AddrPort{to}, c.reported...)
	if len(c.reported) > MaxRemotes {
		c.reported = c.reported[:MaxRemotes]
	}
}
```

**File:** remote_list.go (L577-639)
```go
func (r *RemoteList) unlockedCollect() {
	addrs := r.addrs[:0]
	relays := r.relays[:0]

	for _, c := range r.cache {
		if c.v4 != nil {
			if c.v4.learned != nil {
				u := protoV4AddrPortToNetAddrPort(c.v4.learned)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}

			for _, v := range c.v4.reported {
				if v == nil {
					continue
				}
				u := protoV4AddrPortToNetAddrPort(v)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}
		}

		if c.v6 != nil {
			if c.v6.learned != nil {
				u := protoV6AddrPortToNetAddrPort(c.v6.learned)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}

			for _, v := range c.v6.reported {
				if v == nil {
					continue
				}
				u := protoV6AddrPortToNetAddrPort(v)
				if !r.unlockedIsBad(u) {
					addrs = append(addrs, u)
				}
			}
		}

		if c.relay != nil {
			for _, v := range c.relay.relay {
				relays = append(relays, v)
			}
		}
	}

	dnsAddrs := r.hr.GetAddrs()
	for _, addr := range dnsAddrs {
		if r.shouldAdd == nil || r.shouldAdd(r.vpnAddrs, addr.Addr()) {
			if !r.unlockedIsBad(addr) {
				addrs = append(addrs, addr)
			}
		}
	}

	r.addrs = addrs
	r.relays = relays

}
```
