[1](#0-0) [2](#0-1)

### Citations

**File:** stackslib/src/net/api/getattachmentsinv.rs (L87-102)
```rust
        let mut index_block_hash = None;
        let mut page_indexes = HashSet::new();

        // expect index_block_hash= and page_indexes=
        for (key, value) in form_urlencoded::parse(query_str.as_bytes()) {
            if key == "index_block_hash" {
                index_block_hash = StacksBlockId::from_hex(&value).ok();
            } else if key == "pages_indexes" {
                let pages_indexes_value = value.to_string();
                for entry in pages_indexes_value.split(',') {
                    if let Ok(page_index) = entry.parse::<u32>() {
                        page_indexes.insert(page_index);
                    }
                }
            }
        }
```

**File:** stackslib/src/net/api/getattachmentsinv.rs (L159-168)
```rust
        if page_indexes.len() > MAX_ATTACHMENT_INV_PAGES_PER_REQUEST {
            let msg = format!(
                "Number of attachment inv pages is limited by {} per request",
                MAX_ATTACHMENT_INV_PAGES_PER_REQUEST
            );
            warn!("{msg}");
            return StacksHttpResponse::new_error(&preamble, &HttpBadRequest::new(msg))
                .try_into_contents()
                .map_err(NetError::from);
        }
```
