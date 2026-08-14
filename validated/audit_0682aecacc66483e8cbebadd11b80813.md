### Title
Closing a Bank Balance Wipes Unclaimed Emissions Rewards Without Settlement - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
The reported issue describes a token contract where `transfer()` forces reward settlement but `transferFrom()` does not, letting a user move value away while leaving rewards stranded/unclaimed. The closest reachable analog in marginfi is the `Balance::close()` routine used by `withdraw_all`, `repay_all`, and `close_balance`: it unconditionally zeroes the balance slot — including `emissions_outstanding` — without ever paying out or transferring those accrued emissions, even though the program defines a dedicated error (`CannotCloseOutstandingEmissions`) that is never actually enforced anywhere in the codebase.

### Finding Description
Each `Balance` position tracks unclaimed rewards in `emissions_outstanding`, which accrues over time as the user holds a deposit/liability position: [1](#0-0) 

When a balance is closed, `BalanceImpl::close()` fully overwrites the struct with `Self::empty_deactivated()`, which sets `emissions_outstanding` back to zero: [2](#0-1) [3](#0-2) 

This `close()` is invoked directly — with no prior emissions settlement or check — from `withdraw_all()`: [4](#0-3) 

from `repay_all()`: [5](#0-4) 

and from `close_balance()`: [6](#0-5) 

Notably, the error enum defines `CannotCloseOutstandingEmissions` ("Cannot close balance because of outstanding emissions"), which strongly implies the program was intended to block closing a balance with unclaimed rewards: [7](#0-6) 

However, a repo-wide search shows this error variant is only referenced in `errors.rs` itself (definition and discriminant mapping) and in a test-utility helper file — it is never actually `check!`'d or returned by `withdraw_all`, `repay_all`, or `close_balance`. None of these three instruction paths call any emissions-settlement routine (e.g. `update_emissions`/`settle_emissions`) or check `balance.emissions_outstanding` before invoking `balance.close()`.

### Impact Explanation
Any regular, unprivileged account holder who calls `lending_account_withdraw` with `withdraw_all = true`, `lending_account_repay` with `repay_all = true`, or `lending_account_close_balance` on a position that has accrued but not-yet-claimed `emissions_outstanding` will have that reward balance silently reset to zero. The reward is not transferred to the user, not routed to any protocol account, and not preserved anywhere — it is simply discarded on-chain, unlike interest/asset dust which is explicitly routed to `collected_insurance_fees_outstanding` in the very same functions. This is a permanent, unrecoverable loss of legitimate user funds (unclaimed emissions), directly analogous to the "reward left unclaimed and lost due to missing settlement override" bug class described in the report, and matches the intended protection implied by the unused `CannotCloseOutstandingEmissions` error.

### Likelihood Explanation
Emissions are a first-class, actively used feature in the protocol (bank flags `EMISSIONS_FLAG_BORROW_ACTIVE`/`EMISSIONS_FLAG_LENDING_ACTIVE`), and `withdraw_all`/`repay_all`/`close_balance` are core, frequently used, fully permissionless (account-authority-gated) user actions with no special preconditions beyond normal position management. Any bank with active emissions where a user withdraws or repays their full balance in one step — which is a very common and encouraged user flow ("withdraw_all is the only way to close a Balance") — will trigger this loss. Likelihood is high because it requires no adversarial setup, just ordinary usage of a bank with emissions enabled.

### Recommendation
Before calling `balance.close()` in `withdraw_all`, `repay_all`, and `close_balance`, settle any outstanding emissions: either (a) require `emissions_outstanding == 0` and return `MarginfiError::CannotCloseOutstandingEmissions` if not (forcing the user to claim first), mirroring the seemingly-intended but currently unused error, or (b) automatically flush `emissions_outstanding` into the position's payout mechanism (e.g. transfer to `emissions_destination_account`) as part of the close path, consistent with how asset/liability dust is already reconciled into `collected_insurance_fees_outstanding` in these same functions.

### Proof of Concept
1. Admin enables emissions on a bank (`EMISSIONS_FLAG_LENDING_ACTIVE`) with a nonzero `emissions_rate`.
2. User deposits into that bank and lets time pass so `emissions_outstanding` accrues on their `Balance` (per the emissions accrual logic tied to `last_update`).
3. Before calling any emissions-claim instruction, the user calls `lending_account_withdraw` with `withdraw_all = true` (or `lending_account_repay` with `repay_all = true`, or `lending_account_close_balance`).
4. Internally this calls `BankAccountWrapper::withdraw_all`/`repay_all`/`close_balance`, which call `balance.close()`.
5. Inspect the account afterward: the balance slot is `empty_deactivated()`, i.e. `emissions_outstanding == 0`, with no corresponding token transfer or credit for the previously accrued rewards — the value is gone. Existing tests such as `programs/marginfi/tests/user_actions/close_balance.rs` and `programs/marginfi/tests/misc/regression.rs` (asserting `emissions_outstanding == 0` post-close) confirm this reset-to-zero behavior but do not verify any payout occurred.

### Citations

**File:** type-crate/src/types/user_account.rs (L300-303)
```rust
    /// Unclaimed emissions rewards for this position
    pub emissions_outstanding: WrappedI80F48,
    /// Unix timestamp (u64) of the last emissions calculation for this position
    pub last_update: u64,
```

**File:** type-crate/src/types/user_account.rs (L347-360)
```rust
    pub fn empty_deactivated() -> Self {
        Balance {
            active: 0,
            bank_pk: Pubkey::default(),
            bank_asset_tag: ASSET_TAG_DEFAULT,
            tag: 0,
            _pad0: [0; 4],
            asset_shares: WrappedI80F48::from(I80F48::ZERO),
            liability_shares: WrappedI80F48::from(I80F48::ZERO),
            emissions_outstanding: WrappedI80F48::from(I80F48::ZERO),
            last_update: 0,
            _padding: [0; 1],
        }
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1485-1489)
```rust
    fn close(&mut self) -> MarginfiResult {
        *self = Self::empty_deactivated();

        Ok(())
    }
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1627-1649)
```rust
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

        balance.close()?;

```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1685-1706)
```rust
    pub fn repay_all(&mut self, in_receivership: bool) -> MarginfiResult<(u64, I80F48)> {
        let balance = &mut self.balance;
        let bank = &mut self.bank;

        let total_liability_shares: I80F48 = balance.liability_shares.into();
        let current_liability_amount = bank.get_liability_amount(total_liability_shares)?;
        let current_asset_amount = bank.get_asset_amount(balance.asset_shares.into())?;

        debug!("Repaying all: {}", current_liability_amount,);

        check!(
            current_liability_amount.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoLiabilityFound
        );

        check!(
            current_asset_amount.is_zero_with_tolerance(ZERO_AMOUNT_THRESHOLD),
            MarginfiError::NoLiabilityFound
        );

        balance.close()?;

```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1739-1767)
```rust
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

        let asset_shares: I80F48 = balance.asset_shares.into();
        let liability_shares: I80F48 = balance.liability_shares.into();
        // Counters are incremented in `*_balance_internal` when shares cross
        // `ZERO_AMOUNT_THRESHOLD` upward; match that condition so we don't
        // double-decrement positions that already crossed downward earlier.
        let had_assets = asset_shares.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);
        let had_liabs = liability_shares.is_positive_with_tolerance(ZERO_AMOUNT_THRESHOLD);

        balance.close()?;
```

**File:** programs/marginfi/src/errors.rs (L71-72)
```rust
    #[msg("Cannot close balance because of outstanding emissions")] // 6033
    CannotCloseOutstandingEmissions,
```
