### Title
No panic in `parse_permissioned_burn_instruction` ConfidentialBurn offset walk

### Summary
The offset walk in the `ConfidentialBurn` arm of `parse_permissioned_burn_instruction` guards every indexing operation on `account_indexes[offset]` with a bounds check (`offset < account_indexes.len().saturating_sub(2)` or `saturating_sub(1)`) performed immediately before the index is used, and `offset` is only incremented after a successful bounds-checked access. `saturating_sub` prevents integer-underflow panics even when `account_indexes.len()` is smaller than 1 or 2, so this pattern cannot panic regardless of the values of `has_sysvar`, the proof offsets, or `account_indexes.len()`.

### Finding Description
Each of the five conditional blocks in the arm (lines 115–159 in `transaction-status/src/parse_token/extension/permissioned_burn.rs`) follows the pattern:
```rust
if <cond> && offset < account_indexes.len().saturating_sub(N) {
    ... account_keys[account_indexes[offset] as usize] ...
    offset += 1;
}
```
`saturating_sub` returns 0 rather than underflowing/panicking when `account_indexes.len() < N`, so the comparison `offset < 0` is simply false in that case, and the block is skipped safely. Because `offset` starts at 2 and is only advanced inside a block that already validated `offset < len - N`, the value used to index `account_indexes` on the next line is always < `account_indexes.len()`, satisfying Rust's slice-indexing safety requirement. Additionally, `check_num_token_accounts(account_indexes, 4)` (line 91) already rejects instructions with fewer than 4 accounts before this code executes, further shrinking the space of inputs that reach the offset walk. The trailing `parse_signers(map, offset, ...)` call is passed the final validated `offset`, and (based on its use elsewhere in the file for `Burn`/`BurnChecked` with a fixed offset of 3) is written to tolerate an offset at or beyond the account list length, consistent with the existing repo pattern.

The existing `check_no_panic` test harness already iterates `account_indexes.len()` (via `instruction.accounts`) over `0..20` for both "All Contexts" (offsets 0/0/0, `has_sysvar == false`) and "All Offsets" (offsets 1/2/3, `has_sysvar == true`) cases, which covers the boundary values described in the question (4, 5, 6 accounts) crossed with the two extreme combinations of proof-offset flags. Extending this fuzz to all 8 flag combinations and the same length range would not surface a new panic because the guard is structurally correct — it is not an off-by-one or unchecked-subtraction bug.

### Impact Explanation
No panic is reachable through this code path. There is no scoped impact under any Agave bounty category — this is not an RPC DoS, node panic, or verification bypass.

### Likelihood Explanation
Not applicable — the guarded offset walk is bounds-safe by construction for all `account_indexes.len()` values and all boundary combinations of `has_sysvar`/proof offsets (0, 1, `u8::MAX`), including the edge lengths 4, 5, and 6 called out in the question.

### Recommendation
No fix required for this specific concern. If additional assurance is desired, the existing `check_no_panic` test in the file (lines 202–213) could be extended to fuzz all 8 combinations of proof-offset flags and a wider range of account-index lengths (e.g., 0..8) purely as a regression/defense-in-depth test, but this is not addressing an actual vulnerability.

### Proof of Concept
Not applicable — no panic-inducing input exists. As a defense-in-depth regression test, one could extend `check_no_panic` in `transaction-status/src/parse_token/extension/permissioned_burn.rs` to loop `account_indexes.len()` over `0..8` crossed with all 8 boolean combinations of `(equality_proof_instruction_offset, ciphertext_validity_proof_instruction_offset, range_proof_instruction_offset)` set to `{0, 1, u8::MAX}`, asserting `parse_token` never panics — this test is expected to pass given the current bounds-checked implementation.