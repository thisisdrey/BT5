### Title
`get_asset_shares()` silently returns zero shares when `asset_share_value == 0`, enabling share-free withdrawals/deposits - ([File: programs/marginfi/src/state/bank.rs])

### Summary
`Bank::get_asset_shares()` contains a special case that returns `I80F48::ZERO` whenever `self.asset_share_value` is zero, instead of computing (or erroring on) the real share amount. This mirrors the reported `getSharesByMintedRsTAO()` bug class: a share-conversion function returns `0` instead of the true value when its denominator ("total minted"/"share value") is zero, which callers then use directly to move real token amounts while crediting/debiting zero shares.

### Finding Description
`get_asset_shares` is defined as: [1](#0-0) 

Note the asymmetry with the sibling function `get_liability_shares`, which has no such zero-guard and would instead error via `checked_div` on a zero divisor: [2](#0-1) 

`get_asset_shares` is consumed directly in the core balance-mutation paths of `MarginfiAccount`:
- In `increase_balance_internal` (used for deposits): `asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?` and then `balance.change_asset_shares(asset_shares_increase)?`. [3](#0-2) 
- In `decrease_balance_internal` (used for withdrawals): `asset_shares_decrease = bank.get_asset_shares(asset_amount_decrease)?` and then `balance.change_asset_shares(-asset_shares_decrease)?`. [4](#0-3) 

Because the withdraw/borrow instructions actually move the raw token `amount` requested by the user through the token vault (not the derived share amount), if `asset_share_value` is ever `0`, `decrease_balance_internal` will:
1. Compute `asset_shares_decrease = 0` (via the buggy zero-check),
2. Leave the user's `balance.asset_shares` unchanged (subtracting 0), and
3. Still allow the outer instruction to transfer the requested token amount out of the bank's liquidity vault.

This is the exact bug class described in the report: a share-conversion helper that should reflect a real proportional amount instead returns a hardcoded `0`, and downstream code uses that `0` as if it were the correct value while still moving real tokens.

### Impact Explanation
If a bank ever reaches a state where `asset_share_value == 0` while `total_asset_shares > 0` (e.g., after severe bad-debt/insolvency events that drive per-share asset value to zero, or any future code path that resets `asset_share_value`), any depositor into or withdrawer from that bank would have zero shares credited/debited while real tokens are transferred. On the withdraw side in particular, this means a user could repeatedly withdraw real tokens from the bank's vault without ever decrementing their recorded `asset_shares`, allowing unlimited draining of the vault (direct theft / insolvency) — matching the report's "unlimited token transfer" impact. On the deposit side, users would transfer in real tokens but receive zero recorded shares, permanently losing their deposited value with no accounting.

### Likelihood Explanation
This requires `asset_share_value` to actually reach exactly `0`, which is not expected to occur under normal interest-accrual operation (the share value normally only grows and is not obviously driven to zero by the interest-rate machinery in `interest_rate.rs`, though I was not able to fully trace every code path that mutates `asset_share_value`, including possible bad-debt/bankruptcy or config paths, within the available context). The presence of an explicit zero-guard in `get_asset_shares` — with no equivalent guard on `get_liability_shares` — indicates the developers anticipated `asset_share_value == 0` as a reachable state, which is why this asymmetric defensive branch exists in the first place. Given the uncertainty in confirming the exact trigger path, likelihood should be treated as lower than the original report but the code-level flaw and its consequence when triggered are concrete and match the reported bug class exactly.

### Recommendation
Remove the silent zero-fallback in `get_asset_shares`. If `asset_share_value` can legitimately be zero (e.g., for an uninitialized/fully-insolvent bank), the function should propagate an explicit error (as `get_liability_shares` implicitly does via `checked_div`) rather than returning `I80F48::ZERO`, so that callers in `increase_balance_internal`/`decrease_balance_internal` cannot silently move real token amounts against a zero share delta. Additionally, audit all code paths that can set `asset_share_value` to zero and add invariant checks preventing a bank from reaching `asset_share_value == 0` while it still holds real assets/liquidity.

### Proof of Concept
1. Assume (or force, if reachable via bad-debt/bankruptcy handling) a bank state where `bank.asset_share_value == 0` while `bank.total_asset_shares > 0` and the bank vault still holds real token liquidity.
2. A user with an active balance in that bank calls the withdraw instruction for amount `X` (up to the vault's available liquidity).
3. Internally, `decrease_balance_internal` computes `asset_amount_decrease = X`, then `asset_shares_decrease = bank.get_asset_shares(X)`, which returns `I80F48::ZERO` due to the buggy branch at [5](#0-4) .
4. `balance.change_asset_shares(-0)` leaves the user's `asset_shares` field unchanged, so `bank.change_asset_shares(-0, false)` also leaves `total_asset_shares` unchanged.
5. The withdraw instruction nonetheless transfers `X` real tokens from the bank vault to the user (transfer logic operates on the requested token amount, independent of the returned share delta).
6. The user can repeat step 2–5 indefinitely (their book balance never decreases), draining the bank's vault of all real liquidity.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L243-247)
```rust
    fn get_liability_shares(&self, value: I80F48) -> MarginfiResult<I80F48> {
        Ok(value
            .checked_div(self.liability_share_value.into())
            .ok_or_else(math_error!())?)
    }
```

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

**File:** programs/marginfi/src/state/marginfi_account.rs (L1855-1860)
```rust
        let asset_shares_increase = bank.get_asset_shares(asset_amount_increase)?;
        balance.change_asset_shares(asset_shares_increase)?;
        bank.change_asset_shares(
            asset_shares_increase,
            matches!(operation_type, BalanceIncreaseType::BypassDepositLimit),
        )?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L1949-1951)
```rust
        let asset_shares_decrease = bank.get_asset_shares(asset_amount_decrease)?;
        balance.change_asset_shares(-asset_shares_decrease)?;
        bank.change_asset_shares(-asset_shares_decrease, false)?;
```
