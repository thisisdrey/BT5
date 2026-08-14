### Title
Unprivileged deposits can permanently block bank closure via front-running `lending_pool_close_bank` - (File: `programs/marginfi/src/instructions/marginfi_group/close_bank.rs`)

### Summary
`lending_pool_close_bank` requires the bank to have zero open lending/borrowing positions and zero total asset/liability shares before it can be closed [1](#0-0) . Because deposits into a non-paused bank are permissionless, any unprivileged user can front-run (or simply race) the admin's `lending_pool_close_bank` call with a trivial deposit to keep `lending_position_count` above zero, causing the closure instruction to revert indefinitely. This mirrors the analog bug class from the external report: a state-changing admin operation that removes/closes an entity is blocked by a check that reverts on nonzero balance rather than handling it, and an unprivileged actor can exploit this to prevent the intended state change.

### Finding Description
The closure check enforces:
```
check!(
    bank.lending_position_count == 0 && bank.borrowing_position_count == 0,
    MarginfiError::BankCannotClose,
    ...
);
check!(
    I80F48::from(bank.total_asset_shares).is_zero_with_tolerance(...)
        && I80F48::from(bank.total_liability_shares).is_zero_with_tolerance(...),
    MarginfiError::BankCannotClose
);
``` [2](#0-1) 

If any account holds a balance in the bank at the moment `lending_pool_close_bank` executes, the whole instruction reverts with `BankCannotClose`, exactly as demonstrated in the test suite: `"bank cannot close with open positions"` [3](#0-2) .

`lending_account_deposit` is a fully permissionless, unprivileged-user instruction as long as the bank's operational state is not `Paused`/`ReduceOnly` [4](#0-3) . There is no requirement that a bank be paused before an admin attempts `lending_pool_close_bank`; the documented "Typical Bank Lifecycle" only recommends winding down via `ReduceOnly` before closing, it is not enforced by the program [5](#0-4) . Consequently, in the window between the bank becoming empty and the admin's `lending_pool_close_bank` transaction landing, any unprivileged user can submit (or front-run with) a minimal deposit, bumping `lending_position_count` from 0 to 1 via `increase_balance_internal`'s position-count bookkeeping [6](#0-5) , which causes the subsequent `close_bank` call to fail the position-count check and revert.

### Impact Explanation
This is a permanent-lock/freeze-class issue on the bank-closure state transition rather than a fund-theft issue: an unprivileged attacker can repeatedly grief admin attempts to close a bank, keeping a bank that should be retired open indefinitely (and forcing the group to continue carrying that bank in its `banks` accounting) [7](#0-6) . Impact is bounded: the admin can mitigate by first setting the bank to `Paused`/`ReduceOnly` (which blocks new deposits) before closing, and no user funds are stolen or put at risk — only the closure operation is delayed/blocked.

### Likelihood Explanation
Likelihood is limited because the attack window only exists if the admin does not pause the bank prior to attempting closure, and the vulnerability guide explicitly documents the pause-before-close lifecycle as the intended admin workflow [5](#0-4) . The cost to an attacker (a single minimal deposit) is very low, but the payoff (temporarily blocking a housekeeping admin action) is also low, and admins following documented procedure are not affected.

### Recommendation
Consider either: (1) requiring the bank to be in a state that disallows new deposits (e.g., `Paused`) as a precondition for `lending_pool_close_bank`, closing the front-running window entirely; or (2) documenting/enforcing (via an explicit check) that the admin must pause the bank in the same transaction as the closure attempt, so no unprivileged deposit can land in between.

### Proof of Concept
1. Admin creates and operates a bank; all depositors withdraw fully so `lending_position_count == 0` and `total_asset_shares == 0`.
2. Admin submits a `lending_pool_close_bank` transaction without first pausing the bank.
3. Before that transaction lands, an unprivileged user submits `lending_account_deposit` with a minimal amount into the same bank (as demonstrated for a non-empty bank in [3](#0-2) , adapted to race a close attempt on an already-empty bank).
4. `lending_position_count` becomes 1, causing `lending_pool_close_bank` to revert with `BankCannotClose` per the check at [8](#0-7) .
5. The attacker can repeat this whenever the bank balance returns to zero, indefinitely preventing bank closure.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/close_bank.rs (L12-41)
```rust
pub fn lending_pool_close_bank(ctx: Context<LendingPoolCloseBank>) -> MarginfiResult {
    let mut group = ctx.accounts.group.load_mut()?;
    // Note: Groups created prior to 0.1.2 have a non-authoritative count here, so subtraction
    // without saturation could reduce the count below zero.
    group.banks = group.banks.saturating_sub(1);

    let bank = ctx.accounts.bank.load()?;

    // banks created prior to 0.1.4 can never be closed because we cannot guarantee an accurate
    // position count for those banks.
    check!(
        bank.get_flag(CLOSE_ENABLED_FLAG),
        MarginfiError::BankCannotClose,
        "Only banks created in 0.1.4 and later can close"
    );
    check!(
        bank.lending_position_count == 0 && bank.borrowing_position_count == 0,
        MarginfiError::BankCannotClose,
        "Only banks with no open positions can close"
    );
    check!(
        I80F48::from(bank.total_asset_shares).is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD)
            && I80F48::from(bank.total_liability_shares)
                .is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
        MarginfiError::BankCannotClose
    );
    check!(
        I80F48::from(bank.emissions_remaining).is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
        MarginfiError::BankCannotClose
    );
```

**File:** tests/specs/basic/13_closebank.spec.ts (L81-112)
```typescript
  it("bank cannot close with open positions", async () => {
    const userAcc = users[0].accounts.get(USER_ACCOUNT);
    const amount = new BN(1 * 10 ** ecosystem.tokenADecimals);
    await users[0].mrgnProgram.provider.sendAndConfirm(
      new Transaction().add(
        await depositIx(users[0].mrgnProgram, {
          marginfiAccount: userAcc,
          bank: bankKey,
          tokenAccount: users[0].tokenAAccount,
          amount: amount,
          depositUpToLimit: false,
        })
      )
    );

    const bankAfterDeposit = await program.account.bank.fetch(bankKey);
    assert.equal(bankAfterDeposit.lendingPositionCount, 1);

    await expectFailedTxWithError(
      async () => {
        await groupAdmin.mrgnProgram.provider.sendAndConfirm(
          new Transaction().add(
            await closeBank(groupAdmin.mrgnProgram, {
              bank: bankKey,
            })
          )
        );
      },
      "BankCannotClose",
      6081
    );
  });
```

**File:** guides/ADMIN/BANK_STATE.md (L27-34)
```markdown
### Operational

Normal operations. All user actions are allowed: deposit, borrow, withdraw, repay, and liquidation.

### ReduceOnly

Only withdrawals and repayments are allowed. New deposits and borrows are blocked. This state is
intended for winding down a bank.
```

**File:** guides/ADMIN/BANK_STATE.md (L140-150)
```markdown
## Typical Bank Lifecycle

1. **Creation**: Bank is created in the **Paused** state. The admin configures oracle, risk
   parameters, interest rate curve, and limits.
2. **Go Live**: Admin sets the state to **Operational**. Users can deposit, borrow, etc.
3. **Normal Operation**: The bank operates normally. The admin may adjust limits as needed. If
   `FREEZE_SETTINGS` is set, only limits can change.
4. **Wind Down** (if needed): Admin sets the state to **ReduceOnly**. Users can only withdraw and
   repay. No new positions can be opened.
5. **Closure** (if needed): Once all positions are closed and the bank is empty, the admin can
   close the bank (`CLOSE_ENABLED_FLAG` is already set from creation).
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1866-1884)
```rust

        // Record if the balance was an asset/liability after
        let has_assets =
            I80F48::from(balance.asset_shares).is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);
        let has_liabs = I80F48::from(balance.liability_shares)
            .is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);
        // Increment position counts depending on the before/after state of the balance
        if !had_assets && has_assets {
            bank.increment_lending_position_count();
        }
        if had_assets && !has_assets {
            bank.decrement_lending_position_count();
        }
        if !had_liabs && has_liabs {
            bank.increment_borrowing_position_count();
        }
        if had_liabs && !has_liabs {
            bank.decrement_borrowing_position_count();
        }
```
