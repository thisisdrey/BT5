[1](#0-0) [2](#0-1)

### Citations

**File:** stackslib/src/net/codec.rs (L411-424)
```rust
impl PoxInvData {
    pub fn has_ith_reward_cycle(&self, index: u16) -> bool {
        if index >= self.bitlen {
            return false;
        }

        let idx = index / 8;
        let bit = index % 8;
        let Some(bitvec_entry) = self.pox_bitvec.get(idx as usize) else {
            return false;
        };
        bitvec_entry & (1 << bit) != 0
    }
}
```

**File:** stackslib/src/net/codec.rs (L433-443)
```rust
    fn consensus_deserialize<R: Read>(fd: &mut R) -> Result<PoxInvData, codec_error> {
        let bitlen: u16 = read_next(fd)?;
        if bitlen == 0 || (bitlen as u64) > GETPOXINV_MAX_BITLEN {
            return Err(codec_error::DeserializeError(
                "Invalid PoxInvData bitlen".to_string(),
            ));
        }

        let pox_bitvec: Vec<u8> = read_next_exact::<_, u8>(fd, bitvec_len(bitlen).into())?;
        Ok(PoxInvData { bitlen, pox_bitvec })
    }
```
