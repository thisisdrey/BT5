[1](#0-0)

### Citations

**File:** stackslib/src/net/connection.rs (L804-834)
```rust
        let payload_len_opt = protocol.payload_len(preamble);
        let payload_len = payload_len_opt.expect("BUG: payload length assumed to be known");
        let buf_bytes = self.buf.get(self.message_ptr..).ok_or_else(|| {
            net_error::RecvError(format!("Message ptr {} overran buffer", self.message_ptr))
        })?;

        // reading a payload of known length
        if buf_bytes.len() >= payload_len {
            // definitely have enough data to form a message
            if let Some(ref pubk) = self.public_key {
                protocol.verify_payload_bytes(pubk, preamble, buf_bytes)?;
            }

            // consume the message
            let message_opt = match protocol.read_payload(preamble, buf_bytes) {
                Ok((message, message_len)) => {
                    test_debug!("Got message of {} bytes with {:?}", message_len, preamble);
                    let next_message_ptr = self.message_ptr.checked_add(message_len).ok_or(
                        net_error::OverflowError("Overflowed buffer pointer".to_string()),
                    )?;

                    // begin parsing at the end of this message
                    let data = self.buf.get(next_message_ptr..).ok_or_else(|| {
                        net_error::RecvError(format!(
                            "Next message ptr {next_message_ptr} overran buffer"
                        ))
                    })?;

                    self.message_ptr = 0;
                    self.payload_ptr = 0;
                    self.buf = data.to_vec();
```
