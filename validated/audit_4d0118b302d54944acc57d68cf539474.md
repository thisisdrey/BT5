### Title
Tiny JupLend deposits can round `expected_shares` to zero, causing users to deposit assets but receive no `asset_shares` in return - ([File: programs/marginfi/src/instructions/juplend/deposit.rs])

### Summary
The `juplend_deposit` instruction computes the number of fToken shares a deposit will mint via `expected_shares_for_deposit_from_rates`, which performs floor-division against JupLend's `liquidity_exchange_price` / `token_exchange_price` (both scaled to 1e12). For small deposit amounts relative to these exchange prices, this computation floors to zero, exactly mirroring the H-3 root cause (numerator precision too small relative to denominator precision causing `strategyTokenAmount` to round to 0). The instruction proceeds to transfer the user's underlying tokens and credit the resulting (zero) share amount to the user's marginfi balance, with no revert-on-zero guard.

### Finding Description
`expected_shares_for_deposit_from_rates` is documented and tested to floor to zero for small deposit amounts when exchange prices are not exactly at the 1e12 baseline: [1](#0-0) 

This is explicitly confirmed by unit tests showing that a deposit of `1` (raw unit) yields `0` shares, and that non-exact-multiple deposits round down: [2](#0-1) [3](#0-2) 

In the production `juplend_deposit` handler, `expected_shares` is computed from these same rates, the user's underlying tokens are transferred into the vault, the deposit is executed against JupLend, and the resulting `minted_shares` (matching `expected_shares` via `require_eq!`) is directly credited to the user's marginfi balance via `bank_account.deposit_no_repay`, with no check that the result is non-zero: [4](#0-3) 

If `amount` is small enough that `expected_shares_for_deposit_from_rates` floors to `0`, then:
- The user's underlying tokens are still transferred out of their account and into the bank's liquidity/JupLend vaults (`cpi_transfer_user_to_liquidity_vault`, `cpi_juplend_deposit`).
- `minted_shares` will legitimately equal `0` (JupLend itself floors identically), so `require_eq!(minted_shares, expected_shares, ...)` passes.
- `bank_account.deposit_no_repay(I80F48::from_num(0))` credits the user with zero `asset_shares`.

This is precisely the H-3 pattern: the user's real assets are moved into the protocol/underlying integration, but the user is credited with zero claim (shares) on those assets.

### Impact Explanation
Users lose the value of assets deposited when a deposit amount is small enough (relative to `token_exchange_price`/`liquidity_exchange_price`) to floor to zero shares, since their tokens are transferred to the JupLend integration but they receive no `asset_shares`, hence no ability to withdraw. The lost value increases `total_asset_shares`-backed reserves without a corresponding shares increase, effectively donating value to other depositors/inflating share value — a concrete loss-of-funds condition for the depositing user.

### Likelihood Explanation
This is reachable by any unprivileged user simply calling `juplend_deposit` with a small enough `amount`. The rounding-to-zero condition depends on `liquidity_exchange_price`/`token_exchange_price` deviating from the 1e12 baseline (which happens naturally as interest accrues over time), and on the deposit amount being small relative to those prices — as demonstrated directly by the repository's own tests (`shares_for_deposit_tiny_amount_can_floor_to_zero`, `round_trip_near_zero_amounts`). No special permissions or validator/admin access are required.

### Recommendation
Add an explicit check in `juplend_deposit` (and equivalent flows) requiring the computed/minted shares to be non-zero before transferring funds or crediting the balance, e.g.:
```rust
require!(expected_shares > 0, MarginfiError::MathError);
```
placed immediately after computing `expected_shares`, mirroring the recommended fix in the referenced report (revert if zero strategy/shares would be minted).

### Proof of Concept
1. Let `liquidity_exchange_price` and `token_exchange_price` deviate from the 1e12 baseline (normal state after any interest accrual), e.g. `liquidity_exchange_price = 1_100_000_000_000`.
2. Call `juplend_deposit(amount = 1)` (or any `amount` small enough that `expected_shares_for_deposit_from_rates(amount, liquidity_exchange_price, token_exchange_price) == 0`), as shown to floor to `0` in: [5](#0-4) 
3. The instruction transfers `amount` of underlying from the user to the vault and deposits it into JupLend (real value moved), then credits the user's marginfi balance with `deposit_no_repay(I80F48::from_num(0))` — zero shares.
4. The user's underlying balance decreased by `amount`, but their marginfi `asset_shares` for that bank remain unchanged (or the balance never becomes active), permanently losing access to the deposited value.

### Citations

**File:** programs/juplend-mocks/src/state.rs (L134-156)
```rust
    /// Expected fToken shares minted when depositing `assets` underlying.
    ///
    /// Mirrors JupLend's actual deposit flow: **round down** via the liquidity layer.
    ///
    /// The deposit goes through a two-step conversion in the liquidity layer before
    /// computing shares. The intermediate floor divisions can cause up to 1 unit of
    /// rounding loss vs the naive single-step formula when exchange prices != 1e12.
    ///
    /// Formula (1e12 precision):
    /// ```text
    /// raw   = floor(assets * 1e12 / liquidity_exchange_price)
    /// norm  = floor(raw * liquidity_exchange_price / 1e12)
    /// shares = floor(norm * 1e12 / token_exchange_price)
    /// ```
    /// https://github.com/Instadapp/fluid-solana-programs/blob/830458299be42eaeb6e1fe8fef6aa23444430a10/programs/lending/src/utils/deposit.rs#L68-L86
    #[inline]
    pub fn expected_shares_for_deposit(&self, assets: u64) -> Option<u64> {
        expected_shares_for_deposit_from_rates(
            assets,
            self.liquidity_exchange_price,
            self.token_exchange_price,
        )
    }
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L55-96)
```rust
    let expected_shares = {
        let lending = ctx.accounts.integration_acc_1.load()?;
        // Compute expected shares minted (round-down) using the same math as JupLend.
        expected_shares_for_deposit_from_rates(
            amount,
            lending.liquidity_exchange_price,
            lending.token_exchange_price,
        )
        .ok_or_else(|| error!(MarginfiError::MathError))?
    };

    let pre_f_token_balance = accessor::amount(&ctx.accounts.integration_acc_2.to_account_info())?;

    // Move underlying into the vault and deposit into JupLend.
    ctx.accounts.cpi_transfer_user_to_liquidity_vault(amount)?;
    ctx.accounts.cpi_juplend_deposit(amount, authority_bump)?;

    let post_f_token_balance = accessor::amount(&ctx.accounts.integration_acc_2.to_account_info())?;
    let minted_shares = post_f_token_balance
        .checked_sub(pre_f_token_balance)
        .ok_or_else(|| error!(MarginfiError::MathError))?;

    // Exact match required.
    require_eq!(
        minted_shares,
        expected_shares,
        MarginfiError::JuplendDepositFailed
    );

    {
        let mut bank = ctx.accounts.bank.load_mut()?;
        let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
        let group = ctx.accounts.group.load()?;
        let clock = Clock::get()?;

        let mut bank_account = BankAccountWrapper::find_or_create(
            &ctx.accounts.bank.key(),
            &mut bank,
            &mut marginfi_account.lending_account,
        )?;

        let share_amount = bank_account.deposit_no_repay(I80F48::from_num(minted_shares))?;
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L352-368)
```rust
    #[test]
    fn shares_for_deposit_non_divisible_rounds_down() {
        // floor(7 * 1e12 / 3e12) = floor(2.333...) = 2
        let shares =
            expected_shares_for_deposit_from_rates(7, 1_000_000_000_000, 3_000_000_000_000)
                .unwrap();
        assert_eq!(shares, 2);
    }

    #[test]
    fn shares_for_deposit_tiny_amount_can_floor_to_zero() {
        // With liquidity_price > 1e12, raw floor can hit zero.
        let shares =
            expected_shares_for_deposit_from_rates(1, 1_100_000_000_000, 1_000_000_000_000)
                .unwrap();
        assert_eq!(shares, 0);
    }
```

**File:** programs/marginfi/src/instructions/juplend/local_tests.rs (L454-468)
```rust
    #[test]
    fn round_trip_near_zero_amounts() {
        let l = lending_state(1_200_000_000_000, 1_500_000_000_000);

        // amount=1: deposit floors to 0 shares, redeem of 0 shares = 0
        let shares = l.expected_shares_for_deposit(1).unwrap();
        assert_eq!(shares, 0);
        let redeemed = l.expected_assets_for_redeem(shares).unwrap();
        assert_eq!(redeemed, 0);

        // amount=2: may produce 1 share depending on prices
        let shares = l.expected_shares_for_deposit(2).unwrap();
        let redeemed = l.expected_assets_for_redeem(shares).unwrap();
        assert!(redeemed <= 2);
    }
```
