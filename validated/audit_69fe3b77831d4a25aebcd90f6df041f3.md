[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** stackslib/src/util_lib/bloom.rs (L115-143)
```rust
fn decode_bitfield<R: Read>(fd: &mut R) -> Result<Vec<u8>, codec_error> {
    let encoding: u8 = read_next(fd)?;
    match encoding {
        x if x == BitFieldEncoding::Sparse as u8 => {
            // sparse encoding
            let vec_len: u32 = read_next(fd)?;
            if vec_len > MAX_MESSAGE_LEN.saturating_sub(5) {
                return Err(codec_error::OverflowError("vec_len is too big".into()));
            }
            let num_filled: u32 = read_next(fd)?;

            if !should_use_sparse_encoding(num_filled as usize, vec_len as usize) {
                return Err(codec_error::OverflowError(
                    "Non-sparse bitfield should not use sparse encoding.".into(),
                ));
            }

            let mut ret = vec![0u8; vec_len as usize];
            for _ in 0..num_filled {
                let idx: u32 = read_next(fd)?;
                let slot = ret.get_mut(idx as usize).ok_or_else(|| {
                    codec_error::DeserializeError(format!("Index overflow: {idx} >= {vec_len}"))
                })?;
                let value: u8 = read_next(fd)?;
                *slot = value;
            }

            Ok(ret)
        }
```
