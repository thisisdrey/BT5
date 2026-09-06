Found a concrete analog: `PeerHost::DNS(String, u16)` accepts an arbitrary attacker-supplied hostname string with **no character validation** (no ASCII/CRLF filter), and that hostname is written verbatim, un-escaped, into the raw HTTP request text this node sends to peers.

### Title
CRLF/header injection into outbound peer HTTP requests via unsanitized `PeerHost::DNS` hostname - (File: `stackslib/src/net/http/request.rs`)

### Summary
`HttpRequestPreamble::consensus_serialize` builds the raw HTTP request text by directly writing `self.verb`, `self.path_and_query_str`, and `format!("{}", self.host)` as bytes with no validation that they are free of `\r`/`\n` or otherwise unsafe characters. `PeerHost::from_str`/`from_host_port` (in `stacks-common/src/types/net.rs`) accepts any string as a `PeerHost::DNS(host, port)` when it fails to parse as an IP or `SocketAddr`, imposing no charset restriction on `host`. If a hostname value that ultimately becomes a `PeerHost` (e.g., derived from remotely-supplied `Host:` header re-use, a neighbor-advertised data URL, or handshake data used to build a `PeerHost` for outbound RPC) contains `\r\n`, that value is written raw into the `Host:` header line of the outbound request, corrupting the request framing.

### Finding Description
- `HttpRequestPreamble::consensus_serialize` (`stackslib/src/net/http/request.rs:226-300`) writes:
```
fd.write_all(self.verb.as_bytes())...
fd.write_all(self.path_and_query_str.as_bytes())...
...
fd.write_all(format!("{}", self.host).as_bytes())...
```
with no check that `verb`, `path_and_query_str`, or `host`'s `Display` output is CRLF-free. [1](#0-0) 
- `PeerHost::DNS` stores an unvalidated `String` and its `Display` impl emits it verbatim (`write!(f, "{s}:{p}")`). [2](#0-1) 
- `PeerHost::from_host_port` / `FromStr for PeerHost` fall back to `PeerHost::DNS(host, port)` for any string that isn't a parseable IP/socket address — there is no rejection of control characters such as `\r`/`\n`. [3](#0-2) 

This mirrors the `urllib3` CRLF-injection bug class: the report describes user/attacker-controlled input flowing unsanitized into `putrequest()`'s method argument, allowing CRLF injection into the raw request line/headers. Here the equality broken is "hostname/verb used to build an HTTP request" vs. "hostname/verb proven free of control characters" — the code assumes any string reaching `HttpRequestPreamble` is already safe, but the constructors (`PeerHost::from_host_port`, `HttpRequestPreamble::new_for_peer`) never enforce that.

### Impact Explanation
If an attacker can influence the hostname string that ends up embedded in a `PeerHost` used to build an outbound `StacksHttpRequest` (e.g., a peer-advertised DNS name / data URL propagated through the peer network and later used to construct a request to that peer, or any code path that builds a `PeerHost` from remotely-supplied text), the attacker could inject arbitrary `\r\n`-terminated header lines or even smuggle a second request into the byte stream sent to a peer or proxy. Depending on what sits between the node and its counterpart (e.g. an HTTP-aware load balancer/reverse proxy in front of a peer's RPC port), this could enable request smuggling. Within the scope's own parsing (`stackslib/src/net/http`), a forged/injected header could also affect logging or downstream header semantics on the receiving side.

### Likelihood Explanation
Exploitability is contingent on there being a reachable path where an attacker-controlled DNS hostname string (not an IP, not a value already sanity-checked elsewhere) flows into `PeerHost::from_host_port` and then into `HttpRequestPreamble::new_for_peer`/`consensus_serialize` without an intervening validation step. I was not able to fully trace within the remaining budget whether any in-scope caller passes fully attacker-controlled, unsanitized strings (as opposed to structured types like `StacksBlockId`, `Txid`, `ConsensusHash` whose `Display` outputs are fixed-charset hex) into the `host` field of a `PeerHost` used for outbound requests — all `new_for_peer` call sites I inspected (`getblock.rs`, `gettransaction.rs`, `gettenure*.rs`, `download/epoch2x.rs`, etc.) use `PeerHost` values built from neighbor connection metadata rather than raw untrusted strings in the samples reviewed. This should be verified further; the root defect (missing input validation in `PeerHost`/`HttpRequestPreamble`) is confirmed, but I could not confirm a fully unauthenticated, remote trigger path within the given tool-call budget.

### Recommendation
Validate `verb`, `path_and_query_str`, and `PeerHost::DNS` hostname strings for CRLF/control characters (and restrict to a safe token charset) at construction time — e.g., in `PeerHost::from_host_port`/`FromStr for PeerHost` and in `HttpRequestPreamble::new`/`new_for_peer` — rejecting or percent-encoding any `\r`, `\n`, or other control bytes before they can reach `consensus_serialize`'s raw `write_all` calls.

### Proof of Concept
```rust
// Conceptual PoC using in-scope types
let evil_host = "example.com\r\nX-Injected: 1\r\n\r\nGET /admin HTTP/1.1".to_string();
let peerhost = PeerHost::from_host_port(evil_host, 80); // PeerHost::DNS(evil_host, 80), no validation
let req = HttpRequestPreamble::new_for_peer(
    peerhost,
    "GET".to_string(),
    "/v2/info".to_string(),
);
let mut buf = vec![];
req.consensus_serialize(&mut buf).unwrap();
// `buf` now contains a smuggled/injected extra header + request line
``` [4](#0-3)

### Citations

**File:** stackslib/src/net/http/request.rs (L94-110)
```rust
    pub fn new_for_peer(
        peerhost: PeerHost,
        verb: String,
        path_and_query_str: String,
    ) -> HttpRequestPreamble {
        HttpRequestPreamble {
            version: HttpVersion::Http11,
            verb,
            path_and_query_str,
            host: peerhost,
            content_type: None,
            content_length: None,
            keep_alive: true,
            headers: BTreeMap::new(),
            set_cookie: vec![],
        }
    }
```

**File:** stackslib/src/net/http/request.rs (L226-252)
```rust
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), CodecError> {
        // "$verb $path HTTP/1.${version}\r\n"
        fd.write_all(self.verb.as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all(" ".as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all(self.path_and_query_str.as_bytes())
            .map_err(CodecError::WriteError)?;

        match self.version {
            HttpVersion::Http10 => {
                fd.write_all(" HTTP/1.0\r\n".as_bytes())
                    .map_err(CodecError::WriteError)?;
            }
            HttpVersion::Http11 => {
                fd.write_all(" HTTP/1.1\r\n".as_bytes())
                    .map_err(CodecError::WriteError)?;
            }
        }

        // "User-Agent: $agent\r\nHost: $host\r\n"
        fd.write_all("User-Agent: stacks/3.0\r\nHost: ".as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all(format!("{}", self.host).as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all("\r\n".as_bytes())
            .map_err(CodecError::WriteError)?;
```

**File:** stacks-common/src/types/net.rs (L230-243)
```rust
#[derive(Clone, PartialEq)]
pub enum PeerHost {
    DNS(String, u16),
    IP(PeerAddress, u16),
}

impl fmt::Display for PeerHost {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match *self {
            PeerHost::DNS(ref s, ref p) => write!(f, "{s}:{p}"),
            PeerHost::IP(ref a, ref p) => write!(f, "{}", a.to_socketaddr(*p)),
        }
    }
}
```

**File:** stacks-common/src/types/net.rs (L271-365)
```rust
impl FromStr for PeerHost {
    type Err = Error;

    fn from_str(header: &str) -> Result<PeerHost, Error> {
        // we're looser than the RFC allows for DNS names -- anything that doesn't parse to an IP
        // address will be parsed to a DNS name.
        // try as IP:port
        match header.parse::<SocketAddr>() {
            Ok(socketaddr) => Ok(PeerHost::IP(
                PeerAddress::from_socketaddr(&socketaddr),
                socketaddr.port(),
            )),
            Err(_) => {
                // maybe missing :port
                let hostport = format!("{header}:80");
                match hostport.parse::<SocketAddr>() {
                    Ok(socketaddr) => Ok(PeerHost::IP(
                        PeerAddress::from_socketaddr(&socketaddr),
                        socketaddr.port(),
                    )),
                    Err(_) => {
                        // try as DNS-name:port
                        let host;
                        let port;
                        let parts: Vec<&str> = header.split(':').collect();
                        if parts.is_empty() {
                            return Err(Error::DecodeError(
                                "Failed to parse PeerHost: no parts".to_string(),
                            ));
                        } else if parts.len() == 1 {
                            // no port
                            host = Some(parts[0].to_string());
                            port = Some(80);
                        } else {
                            let np = parts.len();
                            if parts[np - 1].chars().all(char::is_numeric) {
                                // ends in :port
                                let host_str = parts[0..np - 1].join(":");
                                if host_str.is_empty() {
                                    return Err(Error::DecodeError("Empty host".to_string()));
                                }
                                host = Some(host_str);

                                let port_res = parts[np - 1].parse::<u16>();
                                port = match port_res {
                                    Ok(p) => Some(p),
                                    Err(_) => {
                                        return Err(Error::DecodeError(
                                            "Failed to parse PeerHost: invalid port".to_string(),
                                        ));
                                    }
                                };
                            } else {
                                // only host
                                host = Some(header.to_string());
                                port = Some(80);
                            }
                        }

                        match (host, port) {
                            (Some(h), Some(p)) => Ok(PeerHost::DNS(h, p)),
                            (_, _) => Err(Error::DecodeError(
                                "Failed to parse PeerHost: failed to extract host and/or port"
                                    .to_string(),
                            )), // I don't think this is reachable
                        }
                    }
                }
            }
        }
    }
}

impl PeerHost {
    pub fn hostname(&self) -> String {
        match *self {
            PeerHost::DNS(ref s, _) => s.clone(),
            PeerHost::IP(ref a, ref p) => format!("{}", a.to_socketaddr(*p).ip()),
        }
    }

    pub fn port(&self) -> u16 {
        match *self {
            PeerHost::DNS(_, ref p) => *p,
            PeerHost::IP(_, ref p) => *p,
        }
    }

    pub fn from_host_port(host: String, port: u16) -> PeerHost {
        // try as IP, and fall back to DNS
        match host.parse::<IpAddr>() {
            Ok(addr) => PeerHost::IP(PeerAddress::from_ip(&addr), port),
            Err(_) => PeerHost::DNS(host, port),
        }
    }
```
