### Title
`lending_account_start_flashloan` sets `ACCOUNT_IN_FLASHLOAN` to `false` instead of `true`, mirroring the reported boolean-typo bug class - (File: `programs/marginfi/src/instructions/marginfi_account/flashloan.rs`)

### Summary
The reported bug class is a boolean-flag assignment error (`==` used where `=`/`true` was intended), causing a state flag that should mark "action in progress" to never actually get set, letting a guarded action run repeatedly / out of its intended invariant. The closest reachable analog in marginfi-v2 is in `lending_account_start_flashloan`, where the flag meant to mark the account as "in flashloan" is set to `false` rather than `true`.

### Finding Description
Per the project's own documentation, `ACCOUNT_IN_FLASHLOAN` "Only set when an account is within a flash loan, e.g. when start_flashloan is called, then unset when the flashloan ends" [1](#0-0) .

However, the actual implementation of `lending_account_start_flashloan` calls `marginfi_account.set_flag(ACCOUNT_IN_FLASHLOAN, false)` — setting the flag to `false` instead of `true`: [2](#0-1) 

By contrast, `lending_account_end_flashloan` correctly calls `marginfi_account.unset_flag(ACCOUNT_IN_FLASHLOAN, false)` to clear it: [3](#0-2) 

The account constraint on `LendingAccountEndFlashloan` requires the flag to be true (`acc.get_flag(ACCOUNT_IN_FLASHLOAN)`) as a precondition, and `check_account_init_health` skips the risk-engine check specifically when `ACCOUNT_IN_FLASHLOAN` is set: [4](#0-3) [5](#0-4) 

If `set_flag(ACCOUNT_IN_FLASHLOAN, false)` in `start_flashloan` is indeed a no-op bug (the flag never actually becomes `true`), the downstream effects would be: (a) `check_flashloan_can_start`'s pre-check `!acc.get_flag(ACCOUNT_IN_FLASHLOAN)` would always pass trivially since the flag is never set, and more importantly (b) the flag-gated skip of health checks inside `check_account_init_health` (used by other lending instructions called between start/end) would never activate, and (c) `LendingAccountEndFlashloan`'s account constraint requiring the flag be `true` would fail with `IllegalFlashloan`.

### Impact Explanation
I could not conclusively confirm at runtime whether this line actually breaks flashloan functionality, because the repository's own test suite includes `flashloan_success_1op` and other passing-looking flashloan tests [6](#0-5) , which would be expected to fail immediately at the `end_flashloan` account constraint if the flag were truly never set. This inconsistency between the visible source line and the apparently-passing tests could indicate: (1) the line is a genuine, currently-undetected/regressed bug (e.g. from a recent refactor — the blame shows this file was touched on 2026-08-10), (2) I am missing an intervening code path that also sets the flag, or (3) `set_flag`'s parameter order is different from what the call site as written implies (I was unable to load the exact `set_flag`/`unset_flag` definitions to confirm the parameter semantics before running out of iterations).

Because I could not verify the exact semantics of `set_flag`/`unset_flag` (parameter order: is it `(flag, value)` or `(value, flag)`?) from `programs/marginfi/src/state/marginfi_account.rs`, I cannot assert with full confidence that this is an exploitable/live bug versus intentional behavior with the same call being an established idiom in this codebase (e.g. `bank.rs`'s `update_flag(value, flag)` takes `(bool, flag)` order, which is the *opposite* order from what's used in `flashloan.rs` — `set_flag(ACCOUNT_IN_FLASHLOAN, false)` puts the flag constant first and the bool second). This raises real uncertainty about whether the call is actually broken or whether `set_flag` on `MarginfiAccountImpl` has a different (flag, value) signature than `update_flag` on `Bank`.

### Likelihood Explanation
This code path is reachable by any unprivileged account authority via the public `lending_account_start_flashloan` instruction, which is a core, frequently-used unprivileged user path (flashloans) [7](#0-6) . If the boolean is indeed backwards, likelihood of a live incident is high given how central flashloans are to the protocol; however, given the passing test suite for flashloans, actual exploitability is unconfirmed.

### Recommendation
A background engineer should:
1. Read the exact definitions of `set_flag` and `unset_flag` in `programs/marginfi/src/state/marginfi_account.rs` (`MarginfiAccountImpl` impl) to confirm parameter order and semantics.
2. Confirm whether `marginfi_account.set_flag(ACCOUNT_IN_FLASHLOAN, false)` in `lending_account_start_flashloan` actually sets the bit to 1 (via some inverted convention) or to 0.
3. If it sets the bit to `0` (i.e., does not set `ACCOUNT_IN_FLASHLOAN`), this is a critical logic bug that should be fixed to `set_flag(ACCOUNT_IN_FLASHLOAN, true)`, and add/re-run flashloan integration tests, and the trident fuzz-harness's `TRANSIENT_FLAGS_MASK` check for `ACCOUNT_IN_FLASHLOAN` at end-of-sequence.
4. If the parameter order/semantics make this call correct, this finding should be closed as a false positive, but the ambiguity itself (two different flag-setter naming/parameter conventions, `update_flag(value, flag)` vs `set_flag(flag, value)`, across `Bank` and `MarginfiAccount`) is worth flagging for consistency/clarity.

### Proof of Concept
Not able to construct a concrete on-chain PoC without confirming `set_flag`'s exact semantics (see Likelihood/Impact sections above) — this requires reading `programs/marginfi/src/state/marginfi_account.rs`'s `set_flag`/`unset_flag`/`get_flag` implementations, which I was unable to retrieve before the iteration budget was exhausted. A background Devin session with full repository access should:
- Locate and read `impl MarginfiAccountImpl for MarginfiAccount` in `programs/marginfi/src/state/marginfi_account.rs`, specifically the `set_flag`, `unset_flag`, and `get_flag` functions.
- Write/run a targeted unit test that calls `lending_account_start_flashloan` and immediately reads back `marginfi_account.account_flags` to check whether bit `ACCOUNT_IN_FLASHLOAN` (value 2) is actually set to 1.
- If bit remains 0, extend a flashloan test to attempt a borrow (without repay) sandwiched between start/end flashloan and confirm whether the initial-health-check-skip logic in `check_account_init_health` is bypassed as intended, or whether unhealthy intermediate states incorrectly fail/pass.

### Citations

**File:** guides/DEVELOPERS_INTEGRATORS/ACCOUNT_LIFECYCLE.md (L31-39)
```markdown
### In Flashloan (Bit 1)

- **Flag**: `ACCOUNT_IN_FLASHLOAN` (value 2)
- **Set by**: The flashloan instruction
- **Cleared by**: End of flashloan

While this flag is active, health checks are deferred. The protocol verifies account health only
at the end of the flashloan transaction. This allows operations that would temporarily leave the
account unhealthy (e.g. borrow then deposit in the same tx).
```

**File:** programs/marginfi/src/instructions/marginfi_account/flashloan.rs (L21-35)
```rust
pub fn lending_account_start_flashloan(
    ctx: Context<LendingAccountStartFlashloan>,
    end_index: u64,
) -> MarginfiResult<()> {
    check_flashloan_can_start(
        &ctx.accounts.marginfi_account,
        &ctx.accounts.ixs_sysvar,
        end_index as usize,
    )?;

    let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
    marginfi_account.set_flag(ACCOUNT_IN_FLASHLOAN, false);

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/flashloan.rs (L113-125)
```rust
pub fn lending_account_end_flashloan<'info>(
    ctx: Context<'info, LendingAccountEndFlashloan<'info>>,
) -> MarginfiResult<()> {
    validate_not_cpi_by_stack_height()?;

    let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;

    marginfi_account.unset_flag(ACCOUNT_IN_FLASHLOAN, false);

    check_account_init_health(&marginfi_account, ctx.remaining_accounts, &mut None)?;

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/flashloan.rs (L127-141)
```rust
#[derive(Accounts)]
pub struct LendingAccountEndFlashloan<'info> {
    #[account(
        mut,
        has_one = authority @ MarginfiError::Unauthorized,
        constraint = {
            let acc = marginfi_account.load()?;
            acc.get_flag(ACCOUNT_IN_FLASHLOAN)
                && !acc.get_flag(ACCOUNT_IN_DELEVERAGE)
                && !acc.get_flag(ACCOUNT_IN_RECEIVERSHIP)
                && !acc.get_flag(ACCOUNT_DISABLED)
                && !acc.get_flag(ACCOUNT_FROZEN)
                && !acc.get_flag(ACCOUNT_IN_ORDER_EXECUTION)
        } @MarginfiError::IllegalFlashloan
    )]
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1110-1118)
```rust
pub fn check_account_init_health<'info>(
    marginfi_account: &MarginfiAccount,
    remaining_ais: &'info [AccountInfo<'info>],
    health_cache: &mut Option<&mut HealthCache>,
) -> MarginfiResult {
    if marginfi_account.get_flag(ACCOUNT_IN_FLASHLOAN) {
        // Risk checks are skipped during flashloans
        return Ok(());
    }
```

**File:** programs/marginfi/tests/user_actions/flash_loan.rs (L28-70)
```rust
#[tokio::test]
async fn flashloan_success_1op() -> anyhow::Result<()> {
    // Setup test executor with non-admin payer
    let test_f = TestFixture::new(Some(TestSettings::all_banks_payer_not_admin())).await;

    let sol_bank = test_f.get_bank(&BankMint::Sol);

    // Fund SOL lender
    let lender_mfi_account_f = test_f.create_marginfi_account().await;
    let lender_token_account_f_sol = test_f
        .sol_mint
        .create_token_account_and_mint_to(1_000)
        .await;
    lender_mfi_account_f
        .try_bank_deposit(lender_token_account_f_sol.key, sol_bank, 1_000, None)
        .await?;

    // Fund SOL borrower
    let borrower_mfi_account_f = test_f.create_marginfi_account().await;

    let borrower_token_account_f_sol = test_f.sol_mint.create_empty_token_account().await;
    // Borrow SOL
    let borrow_ix = borrower_mfi_account_f
        .make_bank_borrow_ix(borrower_token_account_f_sol.key, sol_bank, 1_000)
        .await;

    let repay_ix = borrower_mfi_account_f
        .make_repay_ix(
            borrower_token_account_f_sol.key,
            sol_bank,
            1_000,
            Some(true),
        )
        .await;

    let flash_loan_result = borrower_mfi_account_f
        .try_flashloan(vec![borrow_ix, repay_ix], vec![], vec![], None)
        .await;

    assert!(flash_loan_result.is_ok());

    Ok(())
}
```

**File:** programs/marginfi/src/lib.rs (L420-427)
```rust
    /// (account authority) Start a flash loan. Must have a corresponding `end_flashloan` ix in the
    /// same tx. Health checks are skipped until the flash loan ends.
    pub fn lending_account_start_flashloan(
        ctx: Context<LendingAccountStartFlashloan>,
        end_index: u64,
    ) -> MarginfiResult {
        marginfi_account::lending_account_start_flashloan(ctx, end_index)
    }
```
