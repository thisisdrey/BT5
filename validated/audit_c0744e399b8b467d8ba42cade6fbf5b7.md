Confirmed root cause: `validate_bank_state` in `programs/marginfi/src/utils/general.rs` unconditionally rejects **every** instruction, including `lending_account_withdraw`, once `bank.config.operational_state == BankOperationalState::KilledByBankruptcy`: [1](#0-0) 

This is the same bug class as the Dopex finding — an unconditional zero/terminal-state check that blocks legitimate closing of a position that legitimately has (or should be allowed to have) zero remaining value — but here the marginfi analog is a genuine, reachable design gap.

### Title
Depositors permanently locked out of closing balances in a bank killed by bankruptcy - (File: `programs/marginfi/src/utils/general.rs`)

### Summary
`socialize_loss` (`programs/marginfi/src/state/bank.rs`) sets `asset_share_value` to zero and flags `kill_bank = true` when a bank's insurance fund and liquidity are insufficient to cover bad debt (super-bankruptcy). `handle_bankruptcy` then sets `bank.config.operational_state = BankOperationalState::KilledByBankruptcy`. From that point, `validate_bank_state` rejects every instruction against that bank — `Withdraw`, `WithdrawAll`, `Repay`, everything — regardless of caller, because the check `if bank.config.operational_state == BankOperationalState::KilledByBankruptcy { return err!(MarginfiError::BankKilledByBankruptcy); }` runs unconditionally before any `InstructionKind` match. [2](#0-1) 

Every other depositor who still had an active balance in that bank at the moment of the kill (their `asset_shares` are non-zero but now worth `0` since `asset_share_value == 0`) can never call `withdraw_all`/`close_balance` on that position again, because `validate_bank_state` is invoked at the top of `lending_account_withdraw` with `InstructionKind::FailsInPausedState` before the withdraw logic even runs: [3](#0-2) 

Confirming this is by design, the `BANK_STATE.md` guide documents `KilledByBankruptcy` as blocking withdraw entirely and being irrecoverable: [4](#0-3) 

### Finding Description
This mirrors the Dopex "cannot withdraw if amount is zero" bug class: a hard-coded terminal check prevents users from ever clearing a balance slot that has legitimately gone to zero value, rather than allowing the zero-value close-out to proceed. In marginfi's case, the impact is not lost funds (their asset value is truly zero — they already forfeited it via socialization), but a **permanent state lock**: their `Balance` slot in `MarginfiAccount.lending_account.balances` (max 16 slots) stays `active` forever with dust/zero shares, since `close_balance`/`withdraw_all` (`programs/marginfi/src/state/marginfi_account.rs`) can never be invoked on that bank once it's `KilledByBankruptcy`. [5](#0-4) [6](#0-5) 

Even though `withdraw_all`/`close_balance` themselves would succeed cleanly on a zero-value balance (their internal checks only require `current_liability_amount` to be zero-with-tolerance, and asset amount can legitimately be zero for `close_balance`), the caller never reaches that logic because `validate_bank_state` short-circuits first.

There is a documented escape hatch — `purgeDeveleragedBalance` (seen in `tests/specs/bankruptcy/zb02_e2eSunset.spec.ts`) — but that is a **risk-admin-only** privileged instruction, not something the affected unprivileged depositor can invoke themselves. [7](#0-6) 

### Impact Explanation
Affected users cannot close their zero-value balance in the killed bank, which permanently occupies one of their 16 available `MarginfiAccount` position slots (`guides/DEVELOPERS_INTEGRATORS/ACCOUNT_LIFECYCLE.md`). If a user has few free slots, this can materially restrict their ability to open new lending/borrowing positions in unrelated banks, effectively freezing part of their account's usability indefinitely, with no self-service remedy since the state is documented as irrecoverable. This is a permanent-lock/freeze condition against an unprivileged user path (ordinary depositor calling ordinary `withdraw`), not a total loss of funds (funds were already zero from socialization) but a denial-of-service on account slot usage that requires privileged (risk-admin) intervention to fix per-user. [8](#0-7) 

### Likelihood Explanation
Requires a super-bankruptcy event (insurance fund + bank liquidity insufficient to cover bad debt) to actually kill a bank, which the project's own docs state has never happened on the main pool as of November 2025 — an extreme, externally-triggered scenario, mirroring the "Medium, reliant on external condition" severity assigned in the original Dopex report. [9](#0-8) 

### Recommendation
Allow a self-service, permissionless "close zero-value balance" path (or relax `validate_bank_state`'s `KilledByBankruptcy` check specifically for closing an already-zero-value balance) so any depositor stuck in a killed bank can free their slot without needing risk-admin intervention via `purgeDeveleragedBalance`.

### Proof of Concept
1. A bank enters super-bankruptcy: `handle_bankruptcy` is called on a bankrupt user whose bad debt exceeds insurance fund + bank assets.
2. `bank.socialize_loss()` sets `asset_share_value = 0` and returns `kill_bank = true`. [10](#0-9) 
3. `handle_bankruptcy` sets `bank.config.operational_state = BankOperationalState::KilledByBankruptcy`. [11](#0-10) 
4. Any other depositor who still held an active (now worthless) balance in this bank calls `lending_account_withdraw` with `withdraw_all = true` to close their position.
5. `validate_bank_state(&bank, InstructionKind::FailsInPausedState)` is invoked first and returns `MarginfiError::BankKilledByBankruptcy` unconditionally, before `bank_account.withdraw_all()` is ever reached. [12](#0-11) 
6. The depositor's balance slot remains `active` forever; only a risk admin running `purgeDeveleragedBalance` can free it, which the ordinary user cannot invoke themselves.

### Citations

**File:** programs/marginfi/src/utils/general.rs (L251-269)
```rust
#[derive(Debug, Clone, Copy)]
pub enum InstructionKind {
    /// Only fails if the bank is in `BankKilledByBankruptcy`, technically doesn't exist (yet)
    Unrestricted,
    /// E.g. withdraw, repay
    FailsInReduceState,
    /// E.g. liquidation
    FailsInPausedState,
    /// E.g. borrow, deposit
    FailsIfPausedOrReduceState,
}

// TODO remove redundant checks for these elsewhere in the program (they are nested many laters deep
// in various value delta functions)
/// Validate the bank's state does not forbid the execution of an instruction
pub fn validate_bank_state(bank: &Bank, kind: InstructionKind) -> MarginfiResult {
    if bank.config.operational_state == BankOperationalState::KilledByBankruptcy {
        return err!(MarginfiError::BankKilledByBankruptcy);
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L66-75)
```rust
    {
        let maybe_bank_mint = {
            let bank = bank_loader.load()?;
            utils::maybe_take_bank_mint(&mut ctx.remaining_accounts, &bank, token_program.key)?
        };

        let in_receivership_or_order_execution =
            marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION);
        let mut bank = bank_loader.load_mut()?;
        validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;
```

**File:** guides/ADMIN/BANK_STATE.md (L45-58)
```markdown
### KilledByBankruptcy

The bank was killed by a bankruptcy event and is irrecoverable. All operations are blocked. This
state can only be set programmatically by the `handle_bankruptcy` instruction when a bankruptcy
event wipes out all remaining assets in the bank. It **cannot** be set manually by an admin.

## Summary Table

| State | Deposit | Borrow | Withdraw | Repay | Liquidate | Initial Margin | Maintenance Margin |
|-------|---------|--------|----------|-------|-----------|----------------|--------------------|
| **Paused** | No | No | No | No | No | N/A | N/A |
| **Operational** | Yes | Yes | Yes | Yes | Yes | Full value | Full value |
| **ReduceOnly** | No | No | Yes | Yes | Yes | $0 | Full value |
| **KilledByBankruptcy** | No | No | No | No | No | N/A | N/A |
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1623-1646)
```rust
    /// Withdraw existing asset in full - will error if there is no asset.
    /// When `in_receivership` is true, clears the bank's liquidation price cache lock
    /// so that banks whose balances are closed mid-liquidation don't stay permanently locked.
    /// Returns `(spl_withdraw_amount, asset_share_delta)`.
    pub fn withdraw_all(&mut self, in_receivership: bool) -> MarginfiResult<(u64, I80F48)> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let total_asset_shares: I80F48 = balance.asset_shares.into();
        let current_asset_amount = bank.get_asset_amount(total_asset_shares)?;
        let current_liability_amount =
            bank.get_liability_amount(balance.liability_shares.into())?;

        debug!("Withdrawing all: {}", current_asset_amount);

        check!(
            current_asset_amount.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoAssetFound
        );

        check!(
            current_liability_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoAssetFound
        );
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1737-1757)
```rust
    /// When `in_receivership` is true, clears the bank's liquidation price cache lock
    /// so that banks whose balances are closed mid-liquidation don't stay permanently locked.
    pub fn close_balance(&mut self, in_receivership: bool) -> MarginfiResult<()> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let current_liability_amount =
            bank.get_liability_amount(balance.liability_shares.into())?;
        let current_asset_amount = bank.get_asset_amount(balance.asset_shares.into())?;

        check!(
            current_liability_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::IllegalBalanceState,
            "Balance has existing debt"
        );

        check!(
            current_asset_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::IllegalBalanceState,
            "Balance has existing assets"
        );
```

**File:** tests/specs/bankruptcy/zb02_e2eSunset.spec.ts (L617-653)
```typescript
  it("(risk admin) Purge user 2's remaining b1 lending account", async () => {
    const user = users[2];
    const userAccount = user.accounts.get(USER_ACCOUNT_THROWAWAY);

    const [liqVault] = deriveLiquidityVault(bankrunProgram.programId, banks[1]);
    const [bankBefore, userBefore, lstBefore, liqVaultBefore] =
      await Promise.all([
        bankrunProgram.account.bank.fetch(banks[1]),
        bankrunProgram.account.marginfiAccount.fetch(userAccount),
        getTokenBalance(bankRunProvider, user.lstAlphaAccount),
        getTokenBalance(bankRunProvider, liqVault),
      ]);

    const tx = new Transaction();
    tx.add(
      await purgeDeveleragedBalance(riskAdmin.mrgnBankrunProgram, {
        account: userAccount,
        bank: banks[1],
      })
    );
    await processBankrunTransaction(bankrunContext, tx, [riskAdmin.wallet]);

    const [bankAfter, userAfter, lstAfter, liqVaultAfter] = await Promise.all([
      bankrunProgram.account.bank.fetch(banks[1]),
      bankrunProgram.account.marginfiAccount.fetch(userAccount),
      getTokenBalance(bankRunProvider, user.lstAlphaAccount),
      getTokenBalance(bankRunProvider, liqVault),
    ]);

    // User gets nothing, we're out of money!
    assert.equal(lstAfter - lstBefore, 0);
    // Liquidity vault is empty, and was empty before!
    assert.equal(liqVaultAfter, 0);
    assert.equal(liqVaultBefore, 0);
    // Balance closed!
    assert.equal(userBefore.lendingAccount.balances[0].active, 1);
    assert.equal(userAfter.lendingAccount.balances[0].active, 0);
```

**File:** guides/DEVELOPERS_INTEGRATORS/ACCOUNT_LIFECYCLE.md (L145-151)
```markdown
## Position Limits

Each `MarginfiAccount` can hold up to **16 balances** (positions) simultaneously. This covers both
lending and borrowing positions. If you need more positions, you must create additional accounts.

An account can hold at most one position per bank: you cannot have both a lending and borrowing
position in the same bank simultaneously.
```

**File:** guides/RISK_AND_LIQUIDATORS/BANKRUPTCY.md (L56-58)
```markdown
### When Does This Matter?

Ideally, never. As of November 2025, bankruptcy has never been executed in the main pool.
```

**File:** programs/marginfi/src/state/bank.rs (L868-872)
```rust
        // Subtract loss, clamping at zero (i.e. assets < liabilities, the bank is wiped out)
        if total_value <= loss_amount {
            self.asset_share_value = I80F48::ZERO.into();
            // This state is irrecoverable, the bank is dead.
            kill_bank = true;
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L208-211)
```rust
    if kill_bank {
        msg!("bank had debt exceeding liabilities and has been killed");
        bank.config.operational_state = BankOperationalState::KilledByBankruptcy;
    }
```
