[File: runtime/near-

### Citations

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L24-24)
```rust
pub(crate) const BLS12381_NOT_IN_GROUP_FIX_VERSION: u32 = 1;
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L37-68)
```rust
        pub fn $fn_name(
            &mut self,
            value_len: u64,
            value_ptr: u64,
            register_id: u64,
        ) -> Result<u64> {
            self.result_state.gas_counter.pay_base($bls12381_base)?;

            let elements_count = value_len / $ITEM_SIZE;
            self.result_state.gas_counter.pay_per($bls12381_element, elements_count as u64)?;

            let data = get_memory_or_register!(self, value_ptr, value_len)?;
            let version = if self.config.bls12381_not_in_group_fix {
                $crate::logic::bls12381::BLS12381_NOT_IN_GROUP_FIX_VERSION
            } else {
                0
            };
            let res_option = super::bls12381::$impl_fn_name(&data, version)?;

            if let Some(res) = res_option {
                self.registers.set(
                    &mut self.result_state.gas_counter,
                    &self.config.limit_config,
                    register_id,
                    res.as_slice(),
                )?;

                Ok(0)
            } else {
                Ok(1)
            }
        }
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L101-120)
```rust
        fn $parse_p(point_data: &[u8], version: u32) -> Option<blst::$blst_p> {
            if point_data[0] & 0x80 != 0 {
                return None;
            }

            let mut pk_aff = blst::$blst_p_affine::default();
            let error_code = unsafe { blst::$blst_p_deserialize(&mut pk_aff, point_data.as_ptr()) };
            let success = error_code == blst::BLST_ERROR::BLST_SUCCESS
                || (version >= $crate::logic::bls12381::BLS12381_NOT_IN_GROUP_FIX_VERSION
                    && error_code == blst::BLST_ERROR::BLST_POINT_NOT_IN_GROUP);
            if !success {
                return None;
            }

            let mut pk = blst::$blst_p::default();
            unsafe {
                blst::$blst_p_from_affine(&mut pk, &pk_aff);
            }
            Some(pk)
        }
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L202-244)
```rust
        pub(crate) fn $p_decompress(data: &[u8], version: u32) -> Result<Option<Vec<u8>>> {
            const ITEM_SIZE: usize = $BLS_P_COMPRESS_SIZE;
            check_input_size(data, ITEM_SIZE, &format!(
