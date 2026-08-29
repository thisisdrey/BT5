No vulnerability found for this question.

The macro instantiation is correctly wired: in the `bls12381_fn!` call for G1 [1](#0-0) , `BLS_FP_SIZE` (48) is passed as `$BLS_FP_SIZE` and `read_fp_point` as `$read_fp_point`, both bound consistently to the G1/Fp path. In the G2 instantiation [2](#0-1) , `BLS_FP2_SIZE` (96) is passed as `$BLS_FP_SIZE` and `read_fp2_point` as `$read_fp_point`, consistently bound to the G2/Fp2 path. Inside the macro body, `$map_fp_to_g`'s `ITEM_SIZE` is defined as `$BLS_FP_SIZE` (the macro parameter, not the global constant) [3](#0-2) , so for `map_fp_to_g1` this resolves to 48 with `read_fp_point`, and for `map_fp2_to_g2` it resolves to 96 with `read_fp2_point`. There is no swap between the two field-size/reader pairs, so `bls12381_map_fp_to_g1` cannot be tricked into reading only half of a 96-byte item — its `ITEM_SIZE` is fixed at 48 and its parser is `read_fp_point`, which is a single-Fp Big-Endian round-trip validator [4](#0-3) .

### Citations

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L246-257)
```rust
        pub(crate) fn $map_fp_to_g(data: &[u8], _version: u32) -> Result<Option<Vec<u8>>> {
            const ITEM_SIZE: usize = $BLS_FP_SIZE;
            check_input_size(data, ITEM_SIZE, $bls12381_map_fp_to_g)?;
            let elements_count: usize = data.len() / ITEM_SIZE;

            let mut res_concat: Vec<u8> = Vec::with_capacity($BLS_P_SIZE * elements_count);

            for item_data in data.chunks_exact(ITEM_SIZE) {
                let fp_point = match $read_fp_point(item_data) {
                    Some(fp_point) => fp_point,
                    None => return Ok(None),
                };
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L273-299)
```rust
bls12381_fn!(
    p1_sum,
    g1_multiexp,
    p1_decompress,
    map_fp_to_g1,
    BLS_P1_SIZE,
    BLS_FP_SIZE,
    BLS_P1_COMPRESS_SIZE,
    blst_p1,
    blst_p1_affine,
    blst_p1_deserialize,
    blst_p1_from_affine,
    blst_p1_cneg,
    blst_p1_add_or_double,
    blst_p1_to_affine,
    blst_p1_affine_serialize,
    blst_p1_in_g1,
    blst_p1_mult,
    read_fp_point,
    blst_map_to_g1,
    blst_p1_uncompress,
    PublicKey,
    parse_p1,
    serialize_p1,
    "bls12381_p1",
    "bls12381_map_fp_to_g1"
);
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L301-327)
```rust
bls12381_fn!(
    p2_sum,
    g2_multiexp,
    p2_decompress,
    map_fp2_to_g2,
    BLS_P2_SIZE,
    BLS_FP2_SIZE,
    BLS_P2_COMPRESS_SIZE,
    blst_p2,
    blst_p2_affine,
    blst_p2_deserialize,
    blst_p2_from_affine,
    blst_p2_cneg,
    blst_p2_add_or_double,
    blst_p2_to_affine,
    blst_p2_affine_serialize,
    blst_p2_in_g2,
    blst_p2_mult,
    read_fp2_point,
    blst_map_to_g2,
    blst_p2_uncompress,
    Signature,
    parse_p2,
    serialize_p2,
    "bls12381_p2",
    "bls12381_map_fp2_to_g2"
);
```

**File:** runtime/near-vm-runner/src/logic/bls12381.rs (L386-404)
```rust
fn read_fp_point(item_data: &[u8]) -> Option<blst::blst_fp> {
    let mut fp_point = blst::blst_fp::default();
    unsafe {
        blst::blst_fp_from_bendian(&mut fp_point, item_data.as_ptr());
    }

    let mut fp_row: [u8; BLS_FP_SIZE] = [0u8; BLS_FP_SIZE];
    unsafe {
        blst::blst_bendian_from_fp(fp_row.as_mut_ptr(), &fp_point);
    }

    for j in 0..BLS_FP_SIZE {
        if fp_row[j] != item_data[j] {
            return None;
        }
    }

    Some(fp_point)
}
```
