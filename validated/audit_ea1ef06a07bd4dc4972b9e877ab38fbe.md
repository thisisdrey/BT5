### Title
Unverified hardcoded CPI instruction name in `drift_claim_bad_debt` risks permanent DoS of bad-debt recovery - (File: `programs/marginfi/src/instructions/drift/claim_bad_debt.rs`)

### Summary
The permissionless instruction `drift_claim_bad_debt` performs a raw CPI into an external, hardcoded program (`MERKLE_DISTRIBUTOR_PROGRAM_ID`) by manually computing an Anchor instruction discriminator from a string literal (`"new_claim"`) rather than using a generated/typed CPI binding. This is the same bug class as the referenced report: a manually specified "function name" used for an external call, with no compile-time or test-time verification that it matches the target program's real instruction name.

### Finding Description
`cpi_new_claim` builds the CPI instruction data using: [1](#0-0) 

This mirrors `get_discrim_hash`, which computes an 8-byte sighash from a `namespace:name` string, exactly like Anchor's `global:<ix_name>` discriminator scheme: [2](#0-1) 

For marginfi's *own* instructions, this same mechanism is exercised by a dedicated regression test, `check_instruction_hash_generated`, that cross-checks every `Hashable::get_hash()` against constants derived from the generated IDL: [3](#0-2) 

That test file even flags the risk explicitly: *"TODO eventually compare these against the generated discrim in the IDL to prevent sausage fingers from changing an ix name and thusly the hash."* [4](#0-3) 

However, `"new_claim"` in `claim_bad_debt.rs` targets an **external, third-party program** (the Drift bad-debt merkle distributor) whose real instruction name is not defined anywhere in this repository, is not covered by any generated IDL binding, and — per the grep results — has zero test coverage anywhere in the codebase (`local_tests.rs` under `drift/` does not exercise this path). Unlike the internal case, there is no mechanism in this repo to catch a mismatch between the hardcoded `"new_claim"` string and the actual on-chain instruction name of the distributor program at `AtXLVASdFhmdq2KZxzhVFonmNXL76dTTsEABXySEHgLh`.

This is structurally identical to the StakedCitadel bug: a caller-side interface (`IVesting.setupVesting`) that doesn't match the callee's real function (`vest`), causing the call to always revert. Here, if `"new_claim"` does not exactly match the deployed distributor's real instruction name (e.g. due to distributor version drift, a private/forked distributor, or a typo), the `invoke_signed` in `cpi_new_claim` will always fail with an unrecognized-discriminator error from the target program.

### Impact Explanation
`drift_claim_bad_debt` is documented as **permissionless** and is the *only* code path in the protocol for recovering a Drift bank's bad-debt portal allocation into the global fee wallet: [5](#0-4) [6](#0-5) 

If the hardcoded discriminator string is wrong (or becomes wrong after any upstream rename of the distributor program), every call to `drift_claim_bad_debt` will revert at the `invoke_signed` step in `cpi_new_claim`, permanently blocking recovery of the allocated bad-debt funds for that bank — analogous to funds being permanently locked, since there is no alternate withdrawal path for this allocation.

### Likelihood Explanation
Medium. This codebase cannot itself confirm or refute whether `"new_claim"` is the correct instruction name for the specific merkle-distributor deployment at `MERKLE_DISTRIBUTOR_PROGRAM_ID`, since that program's source/IDL is external and not vendored here. What is objectively verifiable is that (a) this is a hand-rolled, string-based CPI discriminator with no compile-time type safety, (b) unlike marginfi's internal instructions, there is no regression test comparing this discriminator against a generated IDL, and (c) no test in the repository exercises this CPI path at all — so a wrong or drifted name would go completely undetected until a real on-chain call fails.

### Recommendation
- Vendor the merkle-distributor's IDL (via `declare_program!`, as already done for Kamino in `kamino-mocks/src/lib.rs`) and use its generated, typed CPI client instead of a hand-built `Instruction` with a manually hashed name.
- If a generated client is not feasible, add a unit test that asserts `get_discrim_hash("global", "new_claim")` equals a constant sourced from the actual distributor's published IDL/discriminator table, mirroring `check_instruction_hash_generated` for internal instructions.
- Add integration/mock coverage for `drift_claim_bad_debt` that exercises the CPI against a local mock of the distributor program, so any mismatch is caught pre-deployment rather than as an on-chain, permanent failure.

### Proof of Concept
Not independently reproducible from this repository alone, since the real merkle-distributor program's instruction set is external and not included. The concrete, verifiable defect is the pattern itself: [7](#0-6) 
combined with the total absence of any test asserting this discriminator's correctness (confirmed via repo-wide search for `new_claim`/`NewClaim`/`MerkleDistributor`, which returns matches only within `claim_bad_debt.rs` itself, and no corresponding assertion analogous to `check_instruction_hash_generated`).

### Citations

**File:** programs/marginfi/src/instructions/drift/claim_bad_debt.rs (L212-233)
```rust
    fn cpi_new_claim(&self, amount: u64, proof: Vec<[u8; 32]>) -> MarginfiResult {
        let mut data = get_discrim_hash("global", "new_claim").to_vec();
        NewClaimIxArgs {
            amount_unlocked: amount,
            amount_locked: 0,
            proof,
        }
        .serialize(&mut data)?;

        let ix = Instruction {
            program_id: self.merkle_distributor_program.key(),
            accounts: vec![
                AccountMeta::new(self.distributor.key(), false),
                AccountMeta::new(self.claim_status.key(), false),
                AccountMeta::new(self.from.key(), false),
                AccountMeta::new(self.claimant_token_account.key(), false),
                AccountMeta::new(self.liquidity_vault_authority.key(), true),
                AccountMeta::new_readonly(self.token_program.key(), false),
                AccountMeta::new_readonly(self.system_program.key(), false),
            ],
            data,
        };
```

**File:** programs/marginfi/src/ix_utils.rs (L16-28)
```rust
/// The function of struct discriminator is constructed from these 8 bytes. Typically, the namespace  
/// is "account" or "state". For instructions it's typically "global".
///
/// e.g. for LiquidateStart:
/// ```
///  let discrim = get_function_hash("global", "liquidate_start")
/// ```
pub fn get_discrim_hash(namespace: &str, name: &str) -> [u8; 8] {
    let preimage = format!("{}:{}", namespace, name);
    let mut sighash = [0u8; 8];
    sighash.copy_from_slice(&hash(preimage.as_bytes()).to_bytes()[..8]);
    sighash
}
```

**File:** programs/marginfi/src/ix_utils.rs (L212-214)
```rust
// TODO eventually compare these against the generated discrim in the IDL to prevent sausage fingers
// from changing an ix name and thusly the hash.
#[cfg(test)]
```

**File:** programs/marginfi/src/ix_utils.rs (L271-291)
```rust
    #[test]
    fn check_instruction_hash_generated() {
        // ─── InitLiquidationRecord ───────────────────────────────────────────────
        let got_init = InitLiquidationRecord::get_hash();
        let want_init = ix_discriminators::INIT_LIQUIDATION_RECORD;
        assert_eq!(got_init, want_init);

        // ─── StartLiquidation ────────────────────────────────────────────────────
        let got_start = StartLiquidation::get_hash();
        let want_start = ix_discriminators::START_LIQUIDATION;
        assert_eq!(got_start, want_start);

        // ─── EndLiquidation ──────────────────────────────────────────────────────
        let got_end = EndLiquidation::get_hash();
        let want_end = ix_discriminators::END_LIQUIDATION;
        assert_eq!(got_end, want_end);

        // ─── LendingAccountWithdraw ──────────────────────────────────────────────
        let got_withdraw = LendingAccountWithdraw::get_hash();
        let want_withdraw = ix_discriminators::LENDING_ACCOUNT_WITHDRAW;
        assert_eq!(got_withdraw, want_withdraw);
```

**File:** programs/marginfi/src/lib.rs (L936-945)
```rust
    /// (permissionless) Claim a Drift bad-debt portal allocation for a Drift bank.
    /// The merkle claimant is the bank's liquidity_vault_authority PDA, and claimed tokens are
    /// swept to the global fee wallet's canonical ATA.
    pub fn drift_claim_bad_debt<'info>(
        ctx: Context<'info, DriftClaimBadDebt<'info>>,
        amount: u64,
        proof: Vec<[u8; 32]>,
    ) -> MarginfiResult {
        drift::drift_claim_bad_debt(ctx, amount, proof)
    }
```

**File:** patch-note-drafts/patch-notes-0.1.9.md (L156-163)
```markdown
### Drift

- `drift_claim_bad_debt(amount, proof)` (permissionless) — claims a Drift bad-debt portal
  allocation for a Drift bank. The bank's `liquidity_vault_authority` PDA must be the claimant in
  Drift's merkle tree. The instruction creates the claimant/global-fee ATAs idempotently, prefunds
  the Drift distributor `ClaimStatus` rent from the payer, claims through Drift's merkle distributor,
  sweeps the claimed tokens to the global fee wallet's canonical ATA, and emits
  `DriftClaimBadDebtEvent`.
```
