### Title
Group Rate-Limiter / Deleverage-Limit Bypass via Divergent `remaining_accounts` Parsing (Sequential-Index Health Engine vs. First-Match Oracle Lookup) - ([File: programs/marginfi/src/utils/general.rs])

### Summary
The external Scribe report's root cause is that two different code paths parse the *same* calldata using two different addressing schemes (a hard-coded fixed offset vs. Solidity's calldata-spec offset resolution), letting an attacker construct calldata where the signature-verification path and the commitment/consumption path disagree on what data they operate on. marginfi-v2 has the same structural pattern in how it resolves a bank's oracle account(s) out of the caller-supplied `remaining_accounts` slice: the main risk/health engine walks `remaining_accounts` **sequentially by fixed offset** (matching each active `Balance` in account order), while the USD-denominated rate-limiter/deleverage price lookup resolves the same bank's oracle by **searching for the first occurrence** of the bank's pubkey in the array. These two consumers can therefore disagree about which oracle account belongs to a given bank inside a single instruction whose `remaining_accounts` are fully attacker-controlled.

### Finding Description
`get_remaining_accounts_per_bank` and the position-based iterators (`BankAccountWithCache::load`, `get_tagged_account_health_components`, `compute_has_isolated_liability_flag`, `EmodeConfigIterator`) all consume `remaining_accounts` **sequentially**: they track a running `account_index`, load the account at that exact index, assert `balance.bank_pk == *bank_ai.key`, and then advance the index by `get_remaining_accounts_per_bank(&bank)`. [1](#0-0) 

In contrast, the price used for the group-level USD rate limiter and deleverage withdraw-limit checks is resolved via `oracle_accounts_for_bank`, which instead calls `.position()` to find the **first** account in `remaining_accounts` whose key equals `bank_key`, and treats the following `N` accounts as that bank's oracle group — with no requirement that this be the same contiguous group the sequential health engine used: [2](#0-1) 

This function backs `fetch_asset_price_for_bank_low_bias` / `fetch_unbiased_price_for_bank_with_cache`, which are called directly from withdraw/borrow instruction handlers (`lending_account_withdraw`, `kamino_withdraw`, `solend_withdraw`, `drift_withdraw`, `juplend_withdraw`) to price the withdrawn/borrowed bank for `record_withdrawal_outflow` and `check_deleverage_withdraw_limit`: [3](#0-2) [4](#0-3) 

`record_withdrawal_outflow` uses whatever price it is given to compute the USD `value` and compare it against the group's hourly/daily outflow capacity, only requiring `price > 0`: [5](#0-4) 

Because `remaining_accounts` for these permissionless, user-supplied instructions is entirely constructed by the caller (per the protocol's own integration guide, callers must "include the withdrawn or borrowed bank and its oracle account group in `remaining_accounts`"), an attacker can insert additional bank/oracle-shaped entries ahead of the "real" contiguous group used by the sequential health engine. The first-match lookup in `oracle_accounts_for_bank` will resolve to this attacker-placed entry instead of the position the health engine actually validated, exactly mirroring the Scribe bug class: one code path (health/risk engine) enforces the "true" structure via strict positional/sequential validation, while a second code path (price-for-rate-limit) re-derives the same conceptual data using a different, weaker addressing rule (first-match search) over the identical raw input.

### Impact Explanation
If the oracle price resolved by the first-match search can be made to diverge from the oracle actually validated by the health engine (e.g., because `OraclePriceFeedAdapter::try_from_bank`'s validation of the oracle account against `bank.config.oracle_keys` is not itself sufficient to prevent an attacker-favorable but still "valid" oracle account from being substituted at the wrong array position), an attacker can under-report the USD value of a withdrawal/borrow to `record_withdrawal_outflow` and `check_deleverage_withdraw_limit`. This defeats the group-level and deleverage-level USD outflow caps — controls specifically designed to bound aggregate protocol-wide outflow/bad-debt exposure — allowing outflows well beyond the admin-configured risk limits. This is an unauthorized bypass of a protocol risk-control (a form of unauthorized state change to protocol risk posture), and in stressed conditions it removes a circuit breaker intended to cap systemic loss.

### Likelihood Explanation
`remaining_accounts` is fully attacker-supplied for these instructions (no on-chain enforcement that the array only contains exactly the balances' banks/oracles in the canonical contiguous layout beyond the sequential index checks performed by the health engine, which does not itself constrain what a first-match search over the full array would find). Any unprivileged user calling `lending_account_withdraw`/`lending_account_borrow`/the Kamino/Solend/Drift/JupLend withdraw analogs while group rate limits or deleverage limits are enabled is a reachable trigger path.

### Recommendation
Make `oracle_accounts_for_bank` derive the oracle account slice using the exact same sequential/positional walk used by the main health engine (i.e., pass in the already-resolved index/offset for the bank being acted upon, rather than independently re-searching `remaining_accounts` for the first matching key), so both consumers are provably reading identical byte ranges for the same bank. At minimum, `oracle_accounts_for_bank` should verify there is exactly one occurrence of `bank_key` in `remaining_accounts` (reject if duplicated) and that the resolved oracle accounts' keys strictly match `bank.config.oracle_keys` before use.

### Proof of Concept
Full verification of exploitability requires confirming whether `OraclePriceFeedAdapter::try_from_bank` (in `programs/marginfi/src/state/price.rs`, not available in the indexed context) strictly validates the supplied oracle account keys against `bank.config.oracle_keys` for every `OracleSetup` variant. This determines whether an attacker-inserted "duplicate bank + arbitrary oracle" pair ahead of the real group can carry a manipulated price or is limited to reusing the bank's own legitimate oracle (in which case only a *timing*/staleness divergence, not an arbitrary price, would be achievable). This gap should be verified with a live Devin session that can build a `remaining_accounts` array with:
```
[attacker_chosen_bank_dup_key, attacker_or_stale_oracle_ai, ..., real_bank_key, real_oracle_ai, ...]
```
and call `lending_account_withdraw`/`kamino_withdraw`/etc. with group rate limits enabled, then assert whether `record_withdrawal_outflow`'s computed USD `value` differs from the value the health engine would compute for the same withdrawal — confirming the divergent-parsing bypass end-to-end.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L241-292)
```rust
    pub fn load<'a>(
        lending_account: &'a LendingAccount,
        remaining_ais: &'info [AccountInfo<'info>],
    ) -> MarginfiResult<Vec<BankAccountWithCache<'a, 'info>>> {
        let mut account_index = 0;
        let active_balances: Vec<&Balance> = lending_account
            .balances
            .iter()
            .filter(|balance| balance.is_active())
            .collect();
        let banks_only = remaining_ais.len() == active_balances.len();

        active_balances
            .into_iter()
            .map(|balance| {
                let bank_ai: Option<&AccountInfo<'info>> = remaining_ais.get(account_index);
                if bank_ai.is_none() {
                    msg!("Ran out of remaining accounts at {:?}", account_index);
                    return err!(MarginfiError::InvalidBankAccount);
                }
                let bank_ai = bank_ai.unwrap();
                let bank_al = AccountLoader::<Bank>::try_from(bank_ai)?;
                let bank = bank_al.load()?;

                let num_accounts = if banks_only {
                    1
                } else {
                    get_remaining_accounts_per_bank(&bank)?
                };
                check_eq!(
                    balance.bank_pk,
                    *bank_ai.key,
                    MarginfiError::InvalidBankAccount
                );

                if !banks_only {
                    let end_idx = account_index + num_accounts;
                    require_gte!(
                        remaining_ais.len(),
                        end_idx,
                        MarginfiError::WrongNumberOfOracleAccounts
                    );
                }

                account_index += num_accounts;

                Ok(BankAccountWithCache {
                    bank: bank_al.clone(),
                    balance,
                })
            })
            .collect::<Result<Vec<_>>>()
```

**File:** programs/marginfi/src/utils/general.rs (L386-408)
```rust
/// Locate a bank's oracle information from a properly formatted slice of remaining accounts.
fn oracle_accounts_for_bank<'info>(
    bank_key: &Pubkey,
    bank: &Bank,
    remaining_accounts: &'info [AccountInfo<'info>],
) -> Result<&'info [AccountInfo<'info>]> {
    let accs_needed = get_remaining_accounts_per_bank(bank)? - 1;

    let bank_idx = remaining_accounts
        .iter()
        .position(|ai| ai.key == bank_key)
        .ok_or_else(|| error!(MarginfiError::BankAccountNotFound))?;

    let start = bank_idx + 1;
    let end = start + accs_needed;

    require!(
        end <= remaining_accounts.len(),
        MarginfiError::WrongNumberOfOracleAccounts
    );

    Ok(&remaining_accounts[start..end])
}
```

**File:** programs/marginfi/src/utils/general.rs (L483-511)
```rust
        // Group-level rate limiting: read-only validation + event emission.
        // The admin aggregates events off-chain and calls update_group_rate_limiter.
        if group_rate_limit_enabled {
            check!(price > I80F48::ZERO, MarginfiError::InvalidRateLimitPrice);

            let value = calc_value(
                I80F48::from_num(balance_amount),
                price,
                bank.get_balance_decimals(),
                None,
            )?;
            if group.rate_limiter.hourly.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .hourly
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupHourlyRateLimitExceeded.into());
                }
            }
            if group.rate_limiter.daily.is_enabled() {
                let remaining = group
                    .rate_limiter
                    .daily
                    .effective_remaining_capacity(clock.unix_timestamp);
                if value.to_num::<i64>() > remaining {
                    return Err(MarginfiError::GroupDailyRateLimitExceeded.into());
                }
            }
```

**File:** programs/marginfi/src/instructions/solend/withdraw.rs (L90-110)
```rust

        // Fetch oracle price for rate limiting and deleverage tracking
        let in_receivership = marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP);
        let group_rate_limit_enabled = group.rate_limiter.is_enabled();
        let price = if in_receivership || group_rate_limit_enabled {
            let price = fetch_asset_price_for_bank_low_bias(
                &bank_key,
                &bank,
                &clock,
                ctx.remaining_accounts,
            )?;

            // Validate price is non-zero during liquidation/deleverage to prevent exploits with stale oracles
            if in_receivership {
                check!(price > I80F48::ZERO, MarginfiError::ZeroAssetPrice);
            }

            price
        } else {
            I80F48::ZERO
        };
```

**File:** programs/marginfi/src/instructions/kamino/withdraw.rs (L108-129)
```rust
        let in_receivership_or_order_execution =
            marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP | ACCOUNT_IN_ORDER_EXECUTION);
        // Fetch oracle price for rate limiting and deleverage tracking
        // When group rate limiter is enabled, oracle is required
        let group_rate_limit_enabled = group.rate_limiter.is_enabled();
        let price = if in_receivership_or_order_execution || group_rate_limit_enabled {
            let price = fetch_asset_price_for_bank_low_bias(
                &bank_key,
                &bank,
                &clock,
                ctx.remaining_accounts,
            )?;

            // Validate price is non-zero during liquidation/deleverage to prevent exploits with stale oracles
            if in_receivership_or_order_execution {
                check!(price > I80F48::ZERO, MarginfiError::ZeroAssetPrice);
            }

            price
        } else {
            I80F48::ZERO
        };
```
