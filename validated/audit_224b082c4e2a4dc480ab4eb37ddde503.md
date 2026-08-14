### Title
Deposits into a bank whose `asset_share_value` has collapsed to zero mint no shares while tokens are still transferred, permanently stranding depositor funds - ([File: programs/marginfi/src/state/bank.rs])

### Summary
`BankImpl::get_asset_shares` contains an explicit special case that returns `I80F48::ZERO` whenever `self.asset_share_value == 0` instead of computing `value / asset_share_value`. This state is reachable via `socialize_loss`, which the code comments describe as setting `asset_share_value` to zero and permanently "killing" the bank when losses meet or exceed total deposit value. Because `lending_account_deposit` (via `BankAccountWrapper::deposit`) still transfers real tokens into the liquidity vault and calls `bank.get_asset_shares(amount)` to compute the shares to mint, any deposit made after the bank's share value collapses to zero mints **zero shares** for a nonzero token transfer, silently breaking the SHARE_ACCOUNTING invariant.

### Finding Description
`get_asset_shares` is defined as: [1](#0-0) 

Unlike `get_liability_shares` (which has no such guard), this asset-side function short-circuits to `I80F48::ZERO` whenever `asset_share_value` is exactly zero, avoiding what would otherwise be a division panic/`math_error!()`.

`asset_share_value` reaches zero via `socialize_loss`: [2](#0-1) 

When `total_value <= loss_amount`, `asset_share_value` is clamped to zero and `kill_bank = true` is returned, with the comment explicitly stating "This state is irrecoverable, the bank is dead."

Once a bank is in this state, a subsequent deposit (`lending_account_deposit` → balance increase logic that calls `bank.get_asset_shares(amount)` then `bank.change_asset_shares(shares, ...)`) will:
1. Compute `shares = 0` for any deposited `amount` (per the zero-value guard).
2. Call `change_asset_shares(0, ...)`, which adds `0` to `total_asset_shares` and, critically, skips the deposit-limit check entirely because that branch is gated on `shares.is_positive()`: [3](#0-2) 

3. Meanwhile the token transfer into the liquidity vault proceeds unconditionally as part of the deposit instruction, since nothing in the deposit path checks that minted shares are nonzero.

The net effect: the depositor's tokens land in the liquidity vault, but `total_asset_shares` (and the depositor's own `balance.asset_shares`) do not change, so the deposited value has no corresponding share representation. This breaks the fundamental accounting identity `vault_balance ≈ total_asset_shares * asset_share_value - total_liability_shares * liability_share_value` and the deposit is permanently unrecoverable by the depositor (their share balance shows zero, so `lending_account_withdraw` has nothing to redeem).

### Impact Explanation
This causes permanent loss/stranding of any tokens deposited into a bank after its `asset_share_value` has collapsed to zero. Even though the party economically harmed is typically the depositor themselves (self-inflicted if they deposit into a known-dead bank), the bug represents a genuine invariant violation (SHARE_ACCOUNTING) and a real fund-loss/accounting-mismatch condition: a bank in this dead state silently accepts and strands further deposits instead of rejecting them, and no code path was found in the reachable functions (`get_asset_shares`, `change_asset_shares`, `socialize_loss`) that pauses the bank's operational state or blocks deposits once `kill_bank` is signaled. If any unsuspecting user (not just the "attacker") deposits into such a bank before an admin manually pauses it, their funds are unrecoverable.

### Likelihood Explanation
Reaching this state requires the bank to already be fully insolvent (assets ≤ liabilities) and for `socialize_loss`/bankruptcy handling to have run against it — this is not attacker-controlled state in the sense of instantly forcing it via a single unprivileged call, but relies on pre-existing bankruptcy/liquidation mechanics reaching full write-off. Whether an unprivileged, non-admin actor can single-handedly drive a healthy bank to this exact zero-value state (e.g., via repeated `socialize_loss` on a near-empty bank without any risk-admin action) could not be fully confirmed from the available code — the call sites for `socialize_loss` (e.g., `lending_pool_handle_bankruptcy`) and their permission gating (admin-only vs. `PERMISSIONLESS_BAD_DEBT_SETTLEMENT_FLAG`-gated) were not found within the retrieved context. This is a material uncertainty: if bankruptcy settlement is admin-gated in all reachable production paths, exploitation requires an admin action and would fall outside the unprivileged-attacker scope of this audit.

### Recommendation
- Have `lending_account_deposit` (and any other bank-crediting instruction) reject deposits when `bank.asset_share_value == 0` (i.e., when the bank has been killed by `socialize_loss`), returning an explicit error instead of silently minting zero shares.
- Alternatively/additionally, have `socialize_loss` set `bank.config.operational_state` to a paused/frozen state whenever `kill_bank` is true, so that deposits, borrows, and other value-affecting operations are blocked entirely on a dead bank.
- Remove or guard the zero-value special case in `get_asset_shares` so it surfaces as an explicit error (e.g., `MarginfiError::BankIsDead` or similar) rather than silently returning zero shares for a nonzero value.

### Proof of Concept
Rust unit/integration test plan (`programs/marginfi/src` or `tests/`):
1. Construct a `Bank` with `total_asset_shares > 0`, `asset_share_value = I80F48::ONE`, and a liability such that `socialize_loss(loss_amount)` is invoked with `loss_amount >= total_value`, driving `asset_share_value` to `I80F48::ZERO` and returning `kill_bank = true` (per `programs/marginfi/src/state/bank.rs:858-886`).
2. Record `vault_balance_before` (liquidity vault token balance) and `total_asset_shares_before`.
3. Simulate a deposit of `amount = N` native tokens into this bank via the deposit code path that calls `bank.get_asset_shares(amount)` and `bank.change_asset_shares(shares, false)`.
4. Assert:
   - `bank.get_asset_shares(I80F48::from_num(N))` returns `I80F48::ZERO` (per `programs/marginfi/src/state/bank.rs:249-256`).
   - `bank.total_asset_shares` is unchanged (`total_asset_shares_before`).
   - The liquidity vault balance increased by `N` (tokens transferred).
   - Therefore `vault_balance_after - vault_balance_before != total_asset_shares_after * asset_share_value - total_asset_shares_before * asset_share_value` (i.e., `N != 0`), demonstrating the SHARE_ACCOUNTING invariant is violated and the depositor's `N` tokens are unbacked by any minted shares.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L249-256)
```rust
    fn get_asset_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        if self.asset_share_value == I80F48::ZERO.into() {
            return Ok(I80F48::ZERO);
        }
        Ok(value
            .checked_div(self.asset_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

**File:** programs/marginfi/src/state/bank.rs (L288-316)
```rust
    fn change_asset_shares(
        &mut self,
        shares: I80F48,
        bypass_deposit_limit: bool,
    ) -> MarginfiResult {
        let total_asset_shares: I80F48 = self.total_asset_shares.into();
        self.total_asset_shares = total_asset_shares
            .checked_add(shares)
            .ok_or_else(math_error!())?
            .into();

        if shares.is_positive() && self.config.is_deposit_limit_active() && !bypass_deposit_limit {
            let total_deposits_amount = self.get_asset_amount(self.total_asset_shares.into())?;

            // For Drift banks, deposit_limit is in native decimals but total_deposits_amount
            // is in 9-decimal (DRIFT_SCALED_BALANCE_DECIMALS). We Scale deposit_limit to match.
            let deposit_limit = if self.config.asset_tag == ASSET_TAG_DRIFT {
                scale_drift_deposit_limit(self.config.deposit_limit, self.mint_decimals)?
            } else {
                I80F48::from_num(self.config.deposit_limit)
            };

            if total_deposits_amount >= deposit_limit {
                let deposits_num: f64 = total_deposits_amount.to_num();
                let limit_num: f64 = deposit_limit.to_num();
                msg!("deposits: {:?} deposit lim: {:?}", deposits_num, limit_num);
                return err!(MarginfiError::BankAssetCapacityExceeded);
            }
        }
```

**File:** programs/marginfi/src/state/bank.rs (L858-886)
```rust
    fn socialize_loss(&mut self, loss_amount: I80F48) -> MarginfiResult<bool> {
        let mut kill_bank = false;
        let total_asset_shares: I80F48 = self.total_asset_shares.into();
        let old_asset_share_value: I80F48 = self.asset_share_value.into();

        // Compute total "old" value of shares
        let total_value: I80F48 = total_asset_shares
            .checked_mul(old_asset_share_value)
            .ok_or_else(math_error!())?;

        // Subtract loss, clamping at zero (i.e. assets < liabilities, the bank is wiped out)
        if total_value <= loss_amount {
            self.asset_share_value = I80F48::ZERO.into();
            // This state is irrecoverable, the bank is dead.
            kill_bank = true;
        } else {
            // otherwise subtract then redistribute
            let new_share_value: I80F48 = (total_value - loss_amount)
                .checked_div(total_asset_shares)
                .ok_or_else(math_error!())?;
            self.asset_share_value = new_share_value.into();
            // Sanity check: should be unreachable.
            if new_share_value == I80F48::ZERO {
                kill_bank = true;
            }
        }

        Ok(kill_bank)
    }
```
