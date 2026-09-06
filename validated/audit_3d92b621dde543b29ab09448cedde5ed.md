[1](#0-0) [2](#0-1)

### Citations

**File:** libsigner/src/v0/messages.rs (L992-1001)
```rust
        let content_len: u32 = read_next(fd)?;
        if content_len > STATE_MACHINE_UPDATE_MAX_SIZE {
            return Err(CodecError::DeserializeError(format!(
                "Message length exceeded max: {STATE_MACHINE_UPDATE_MAX_SIZE}"
            )));
        }
        let buffer_len = usize::try_from(content_len)
            .expect("FATAL: cannot process signer messages when usize < u32");
        let mut buffer = vec![0u8; buffer_len];
        fd.read_exact(&mut buffer).map_err(CodecError::ReadError)?;
```
