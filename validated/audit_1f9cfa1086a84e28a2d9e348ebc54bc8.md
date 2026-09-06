### No vulnerability found for this question.

The `new` function at [1](#0-0)  is a trivial constructor that only takes a locally-configured `auth: Option<String>` password and initializes empty/default fields (`block_id: None`, `transactions: vec![]`, `mint: vec![]`). It performs no parsing, no length reads, no buffer indexing, and receives no attacker-controlled bytes whatsoever — it is called during handler setup, not during request processing.

All parsing of attacker-controlled wire data for this endpoint happens instead in `try_parse_request` [2](#0-1) , which is gated behind an authorization header check against a configured secret password [3](#0-2)  — this endpoint requires the node's configured secret, which per the rules is out of scope for an unprivileged attacker. Additionally, that parsing path enforces `MAX_PAYLOAD_LEN` and delegates transaction decoding to `StacksTransaction::consensus_deserialize`, a length-checked codec routine [4](#0-3) .

Since the target function `new` performs no wire-data handling at all, there is no reachable OOB-length read in it, and the actual request-parsing path requires possession of the configured auth secret, which is excluded by the rules for an unprivileged attacker.

### Citations

**File:** stackslib/src/net/api/blocksimulate.rs (L59-67)
```rust
    pub fn new(auth: Option<String>) -> Self {
        Self {
            block_id: None,
            auth,
            profiler: false,
            transactions: vec![],
            mint: vec![],
        }
    }
```

**File:** stackslib/src/net/api/blocksimulate.rs (L145-213)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        // If no authorization is set, then the block replay endpoint is not enabled
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }

        let block_id_str = captures
            .name("block_id")
            .ok_or_else(|| {
                Error::DecodeError("Failed to match path to block ID group".to_string())
            })?
            .as_str();

        let block_id = StacksBlockId::from_hex(block_id_str)
            .map_err(|_| Error::DecodeError("Invalid path: unparseable block id".to_string()))?;

        self.block_id = Some(block_id);

        if let Some(query_string) = query {
            for (key, value) in form_urlencoded::parse(query_string.as_bytes()) {
                if key == "profiler" {
                    if value == "1" {
                        self.profiler = true;
                        break;
                    }
                }
            }
        }

        if preamble.get_content_length() == 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected non-zero-length body for block proposal endpoint"
                    .to_string(),
            ));
        }
        if preamble.get_content_length() > MAX_PAYLOAD_LEN {
            return Err(Error::DecodeError(
                "Invalid Http request: BlockProposal body is too big".to_string(),
            ));
        }

        (self.transactions, self.mint) = match preamble.content_type {
            Some(HttpContentType::JSON) => Self::parse_json(body)?,
            Some(_) => {
                return Err(Error::DecodeError(
                    "Wrong Content-Type for block proposal; expected application/json".to_string(),
                ))
            }
            None => {
                return Err(Error::DecodeError(
                    "Missing Content-Type for block simulation".to_string(),
                ))
            }
        };

        Ok(HttpRequestContents::new().query_string(query))
    }
```
