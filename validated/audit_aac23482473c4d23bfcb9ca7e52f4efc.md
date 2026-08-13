### Title
JupLend deposit rounds `expected_shares` to zero on small deposits, permanently burning user funds - ([File: programs/marginfi/src/instructions/juplend/deposit.rs])

### Summary
`juplend_deposit` computes the fTokens a user should receive via `expected_shares_for_deposit_from_rates`, a floor-division formula identical in structure to the reported `curvePriceWad` rounding bug. When `amount` is small relative to `liquidity_exchange_price`/`token_exchange_price`, the computation can floor to `0`, but the function still transfers the user's underlying tokens into the vault and mints/credits the (zero) result as the user's marginfi position, permanently losing the deposited principal.

### Finding Description
`juplend_deposit` calls:
```rust
let expected_shares = expected_shares_for_deposit_from_rates(
    amount,
    lending.liquidity_exchange_price,
    lending.token_exchange_price,
).ok_or_else(|| error!(MarginfiError::MathError))?;
``` [1](#0-0) 

The helper performs a two-step floor division mirroring JupLend's math:
```rust
let registered_amount_raw = (assets as u128)
    .checked_mul(EXCHANGE_PRICES_PRECISION)?
    .checked_div(liquidity_ex_price)?;
let registered_amount = registered_amount_raw
    .checked_mul(liquidity_ex_price)?
    .checked_div(EXCHANGE_PRICES_PRECISION)?;
let shares_u128 = registered_amount
    .checked_mul(EXCHANGE_PRICES_PRECISION)?
    .checked_div(token_ex_price)?;
``` [2](#0-1) 

The code only guards against `token_exchange_price == 0` or `liquidity_exchange_price == 0` (returning `None` → `MathError`), but does **not** guard against the quotient itself rounding to `0`. The repo's own unit tests confirm this is reachable:
```rust
fn shares_for_deposit_tiny_amount_can_floor_to_zero() {
    // With liquidity_price > 1e12, raw floor can hit zero.
    let shares =
        expected_shares_for_deposit_from_rates(1, 1_100_000_000_000, 1_000_000_000_000)
            .unwrap();
    assert_eq!(shares, 0);
}
``` [3](#0-2) 

When `expected_shares == 0`, `juplend_deposit` still proceeds: it transfers the user's underlying tokens to the vault, CPIs into JupLend's `deposit`, and — because JupLend's own math should independently also floor to `0` fTokens minted — the `require_eq!(minted_shares, expected_shares, ...)` check passes (both zero) rather than reverting:
```rust
ctx.accounts.cpi_transfer_user_to_liquidity_vault(amount)?;
ctx.accounts.cpi_juplend_deposit(amount, authority_bump)?;
...
require_eq!(minted_shares, expected_shares, MarginfiError::JuplendDepositFailed);
...
let share_amount = bank_account.deposit_no_repay(I80F48::from_num(minted_shares))?;
``` [4](#0-3) 

The result: the user's underlying tokens are pulled out of their wallet and deposited into the external JupLend pool, but the marginfi account is credited with `0` asset shares — the deposit is silently absorbed with no corresponding claim, an unrecoverable loss of the user's principal. This is the direct structural analog of the reported bug: a floor-division price/rate calculation rounding to zero, propagated through a "success" path (rather than reverting), producing a mismatch between value transferred and value credited.

### Impact Explanation
Any unprivileged user who deposits a sufficiently small `amount` into a JupLend-backed bank (or is induced to via a wallet/integration passing a small dust amount) into an integration whose exchange prices are not exactly `1e12` can have their tokens transferred and irrevocably lost — the account has no share credited, so there is no `withdraw` path to recover them. This is a direct loss-of-user-funds condition, distinct from a normal precision-loss/dust issue, because the entire deposited amount (not just a rounding remainder) disappears.

### Likelihood Explanation
This requires the JupLend integration's `token_exchange_price`/`liquidity_exchange_price` to have drifted away from the `1e12` baseline (which happens naturally as yield accrues over time) and the user (or an integrator/router building on top of marginfi) to submit a small enough `amount`. The repository's own test suite (`jlr11_rounding_loop.spec.ts`, `shares_for_deposit_tiny_amount_can_floor_to_zero`) demonstrates this floor-to-zero condition is reachable and even actively searched for, indicating it is a realistic, not purely theoretical, state. Likelihood is moderate: it needs a specific combination of exchange-price drift and small deposit size, but no special privileges are required to trigger it.

### Recommendation
Add an explicit check in `juplend_deposit` (and any equivalent JupLend integration path) requiring `expected_shares > 0` before transferring the user's underlying tokens and proceeding with the CPI deposit, e.g.:
```rust
require!(expected_shares > 0, MarginfiError::DepositAmountTooSmall);
```
Alternatively, enforce a minimum deposit `amount` such that `expected_shares_for_deposit_from_rates` can never floor to zero at the current exchange prices.

### Proof of Concept
1. Allow the JupLend integration's `liquidity_exchange_price`/`token_exchange_price` to drift from `1e12` (naturally happens as yield accrues, as modeled in `jlr11_rounding_loop.spec.ts`).
2. Call `juplend_deposit` with a small `amount` (e.g., `amount = 1`) such that:
```rust
expected_shares_for_deposit_from_rates(1, 1_100_000_000_000, 1_000_000_000_000) == 0
```
as confirmed by the repo's own test [3](#0-2) .
3. `juplend_deposit` transfers the user's `1` unit of underlying to the vault and CPIs into JupLend's deposit, then credits `bank_account.deposit_no_repay(I80F48::from_num(0))` — the user has `0` shares and no way to reclaim the transferred token, while the underlying is now custodied in the JupLend pool with no matching marginfi position.

### Citations

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L55-64)
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
```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L68-96)
```rust
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

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L361-368)
```rust
    #[test]
    fn shares_for_deposit_tiny_amount_can_floor_to_zero() {
        // With liquidity_price > 1e12, raw floor can hit zero.
        let shares =
            expected_shares_for_deposit_from_rates(1, 1_100_000_000_000, 1_000_000_000_000)
                .unwrap();
        assert_eq!(shares, 0);
    }
```

**File:** programs/juplend-mocks/src/state.rs (L71-83)
```rust
    let registered_amount_raw = (assets as u128)
        .checked_mul(EXCHANGE_PRICES_PRECISION)?
        .checked_div(liquidity_ex_price)?;

    let registered_amount = registered_amount_raw
        .checked_mul(liquidity_ex_price)?
        .checked_div(EXCHANGE_PRICES_PRECISION)?;

    let shares_u128 = registered_amount
        .checked_mul(EXCHANGE_PRICES_PRECISION)?
        .checked_div(token_ex_price)?;

    shares_u128.try_into().ok()
```
