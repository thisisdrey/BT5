### No vulnerability found for this question.

`new_getclaritymarf` is a client-side constructor that builds an *outgoing* HTTP request from already-validated local Rust values (`host: PeerHost`, `marf_key_hash: TrieHash`, `tip_req: TipRequest`, `with_proof: bool`); it does not parse or decode any attacker-supplied wire bytes, so there is no length-prefixed field being allocated on an unchecked wire value here at all [1](#0-0) .

The actual server-side parsing of remote bytes for this endpoint happens in `try_parse_request`, which matches the URL path against a regex requiring exactly `[0-9a-f]{64}` before calling `TrieHash::from_hex`, a fixed 32-byte hash with no attacker-controlled length or count driving an allocation, and it also rejects any non-zero body length up front [2](#0-1) . There is no count/length field read from the wire that sizes a `Vec` or buffer allocation in this file.

### Citations

**File:** stackslib/src/net/api/getclaritymarfvalue.rs (L68-92)
```rust
    fn try_parse_request(
        &mut self,
        preamble: &HttpRequestPreamble,
        captures: &Captures,
        query: Option<&str>,
        _body: &[u8],
    ) -> Result<HttpRequestContents, Error> {
        if preamble.get_content_length() != 0 {
            return Err(Error::DecodeError(
                "Invalid Http request: expected 0-length body".to_string(),
            ));
        }

        let marf_key = if let Some(key_str) = captures.name("marf_key_hash") {
            TrieHash::from_hex(key_str.as_str())
                .map_err(|e| Error::Http(400, format!("Invalid hash string: {e:?}")))?
        } else {
            return Err(Error::Http(404, "Missing `marf_key_hash`".to_string()));
        };

        self.marf_key_hash = Some(marf_key);

        let contents = HttpRequestContents::new().query_string(query);
        Ok(contents)
    }
```

**File:** stackslib/src/net/api/getclaritymarfvalue.rs (L188-205)
```rust
impl StacksHttpRequest {
    pub fn new_getclaritymarf(
        host: PeerHost,
        marf_key_hash: TrieHash,
        tip_req: TipRequest,
        with_proof: bool,
    ) -> StacksHttpRequest {
        StacksHttpRequest::new_for_peer(
            host,
            "GET".into(),
            format!("/v2/clarity/marf/{}", &marf_key_hash),
            HttpRequestContents::new()
                .for_tip(tip_req)
                .query_arg("proof".into(), if with_proof { "1" } else { "0" }.into()),
        )
        .expect("FATAL: failed to construct request from infallible data")
    }
}
```
