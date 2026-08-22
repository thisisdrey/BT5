### Title
`isSelfNebulaOrLocalhost` trusts the UDP source address of an unauthenticated, non-overlay socket, allowing an unprivileged remote attacker to spoof access to TXT/`QueryCert` responses - ([File: dns_server.go])

### Summary
`dnsServer.isSelfNebulaOrLocalhost` gates TXT-record (certificate disclosure) queries by parsing `w.RemoteAddr().String()` from a `dns.Server` bound with `Net: "udp"` and checking whether the parsed IP falls in `cs.myVpnAddrsTable`. Because the DNS listener is a plain OS UDP socket (default `lighthouse.dns.host` is empty, i.e. binds to all interfaces, `:53`), the "source" it observes is the IP header's claimed source, which is not cryptographically bound to nebula's overlay identity and can be forged by any attacker capable of UDP source-address spoofing on the path to the host.

### Finding Description
The DNS responder is started via `dns.Server{..., Net: "udp", ...}` and `server.ListenAndServe()` [1](#0-0) , which is a normal kernel UDP socket, not a wrapper around nebula's encrypted/authenticated tunnel. `getDnsServerAddr` defaults `lighthouse.dns.host` to `""`, producing an address of `:53` (all interfaces) [2](#0-1) , so the socket can receive packets on the host's real, internet-facing interface, not only via the nebula tun device.

`parseQuery` uses `isSelfNebulaOrLocalhost(w.RemoteAddr().String())` as the sole gate before answering TXT queries (which call `QueryCert`, disclosing certificate material for self or any known peer) [3](#0-2) . The gate itself only parses the address string and checks loopback or membership in the local VPN address table: [4](#0-3) 

Because the address comes from a raw UDP packet's IP source field with no cryptographic verification (no nebula handshake, no relayed/authenticated tunnel involvement), an attacker who can spoof a UDP source IP (many networks still permit this despite BCP38) can send a packet to the host's real address on port 53 with source IP set to any address inside the CIDR tracked by `cs.myVpnAddrsTable` (i.e., an address that looks like a nebula overlay IP). The kernel delivers the packet to the listening socket regardless of interface, `w.RemoteAddr()` reflects the forged source, and `isSelfNebulaOrLocalhost` returns `true`, incorrectly treating the request as having originated from inside the nebula overlay.

This breaks the intended invariant documented in the code itself ("We only answer these queries from nebula nodes or localhost") because "nebula node" is being inferred from an unauthenticated IP string rather than from actual delivery through nebula's encrypted/authenticated tunnel path.

### Impact Explanation
Successful exploitation allows an unprivileged remote attacker (no cert, no keys, no host access) to bypass the access-control gate for TXT queries and obtain `QueryCert` results, disclosing the certificate (including nebula identity/name/VPN addresses) of the lighthouse itself or of any peer currently in its hostmap [5](#0-4) . This is an authentication/authorization bypass of a gating check intended to restrict sensitive disclosure to overlay-verified peers, matching an "unauthorized information disclosure via authentication bypass" bounty category.

### Likelihood Explanation
- Requires the lighthouse to run with `lighthouse.serve_dns` enabled and `lighthouse.am_lighthouse` true (feature-gated) [6](#0-5) .
- Requires the DNS listener to be reachable on the host's real (non-tun) network path, which is the default configuration (`lighthouse.dns.host` defaults to `""`, i.e., binds all interfaces) [2](#0-1) .
- Requires the attacker to be able to spoof a UDP source address that reaches the target, which is network/path dependent (blocked by strict egress/ingress filtering on many networks, but not universally).
- No cryptographic material, prior handshake, or host access is needed by the attacker, and the technique is repeatable per query.

### Recommendation
Do not derive trust from the UDP source address at all. Options:
1. Bind the DNS listener strictly to a nebula tun-assigned address (never `0.0.0.0`/`::`) so it is only reachable through the nebula overlay's own routing, and reject/refuse to start if `lighthouse.dns.host` resolves to a wildcard/non-tun address.
2. Better, don't rely on socket-level addressing at all for TXT/cert disclosure; require the requester to present a valid nebula identity (e.g., only answer TXT queries received over an internal channel fed by decrypted/authenticated overlay traffic, or require callers to prove overlay membership via the handshake state, not `w.RemoteAddr()`).
3. At minimum, explicitly validate at startup that the DNS server cannot be reached from outside the tun interface (e.g., bind exclusively to tun IPs, and additionally have the OS/firewall drop non-tun-sourced packets destined to that port), and document/enforce this instead of just relying on the address string.

### Proof of Concept
Unit test plan (extends existing `dns_server` test file conventions):
```go
func TestIsSelfNebulaOrLocalhost_SpoofedSource(t *testing.T) {
    // Build a dnsServer with a certState whose myVpnAddrsTable contains 192.168.100.5/24 (a nebula VPN CIDR)
    ds := newTestDnsServer(t, /* cert with vpn addr 192.168.100.5 */)

    // Attacker crafts an address string mimicking a spoofed UDP packet
    // whose IP source header claims to be a nebula VPN address, despite
    // never having gone through a nebula handshake or the tun device.
    spoofedAddr := "192.168.100.5:40000"

    got := ds.isSelfNebulaOrLocalhost(spoofedAddr)

    // Expected (buggy) behavior: returns true purely because the string
    // matches the VPN table, with no cryptographic/tunnel verification.
    if !got {
        t.Fatalf("expected spoofed nebula source string to incorrectly pass the gate")
    }
}
```
Integration-level confirmation: start `dnsServer.Start()` with `lighthouse.dns.host` left at its default (empty → binds `:53` on all interfaces), then from a separate raw-socket test harness send a UDP DNS TXT query to the host's non-tun interface with the IP source header forged to a `myVpnAddrsTable` address, and assert the server responds with `QueryCert` data instead of dropping the query — demonstrating the gate is bypassable without ever traversing the nebula overlay.

### Citations

**File:** dns_server.go (L94-96)
```go
	wantsDns := c.GetBool("lighthouse.serve_dns", false)
	amLighthouse := c.GetBool("lighthouse.am_lighthouse", false)
	enabled := wantsDns && amLighthouse
```

**File:** dns_server.go (L171-176)
```go
	server := &dns.Server{
		Addr:              addr,
		Net:               "udp",
		Handler:           d.mux,
		NotifyStartedFunc: func() { close(started) },
	}
```

**File:** dns_server.go (L252-290)
```go
func (d *dnsServer) QueryCert(data string) string {
	if len(data) < 2 {
		return ""
	}
	ip, err := netip.ParseAddr(data[:len(data)-1])
	if err != nil {
		return ""
	}

	// The hostmap only ever contains peers we have handshaked with, so it never carries an entry for ourselves.
	// Answer self lookups straight from the local cert state.
	if cs := d.certState(); cs != nil && cs.myVpnAddrsTable != nil && cs.myVpnAddrsTable.Contains(ip) {
		c := cs.GetDefaultCertificate()
		if c == nil {
			return ""
		}
		b, err := c.MarshalJSON()
		if err != nil {
			return ""
		}
		return string(b)
	}

	hostinfo := d.hostMap.QueryVpnAddr(ip)
	if hostinfo == nil {
		return ""
	}

	q := hostinfo.GetCert()
	if q == nil {
		return ""
	}

	b, err := q.Certificate.MarshalJSON()
	if err != nil {
		return ""
	}
	return string(b)
}
```

**File:** dns_server.go (L372-389)
```go
func (d *dnsServer) isSelfNebulaOrLocalhost(addr string) bool {
	a, _, _ := net.SplitHostPort(addr)
	b, err := netip.ParseAddr(a)
	if err != nil {
		return false
	}

	if b.IsLoopback() {
		return true
	}

	cs := d.certState()
	if cs == nil || cs.myVpnAddrsTable == nil {
		return false
	}
	//if we found it in this table, it's good
	return cs.myVpnAddrsTable.Contains(b)
}
```

**File:** dns_server.go (L415-429)
```go
		case dns.TypeTXT:
			// We only answer these queries from nebula nodes or localhost
			if !d.isSelfNebulaOrLocalhost(w.RemoteAddr().String()) {
				return
			}
			if debugEnabled {
				d.l.Debug("DNS query", "type", "TXT", "name", q.Name)
			}
			ip := d.QueryCert(q.Name)
			if ip != "" {
				rr, err := dns.NewRR(fmt.Sprintf("%s TXT %s", q.Name, ip))
				if err == nil {
					m.Answer = append(m.Answer, rr)
				}
			}
```

**File:** dns_server.go (L451-458)
```go
func getDnsServerAddr(c *config.C) string {
	dnsHost := strings.TrimSpace(c.GetString("lighthouse.dns.host", ""))
	// Old guidance was to provide the literal `[::]` in `lighthouse.dns.host` but that won't resolve.
	if dnsHost == "[::]" {
		dnsHost = "::"
	}
	return net.JoinHostPort(dnsHost, strconv.Itoa(c.GetInt("lighthouse.dns.port", 53)))
}
```
