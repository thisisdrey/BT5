## Title
Unprotected external-exchange-rate deposits (Kamino/Juplend/Drift/Solend) expose depositors to unbounded slippage from reserve manipulation - (File: `programs/marginfi/src/instructions/kamino/deposit.rs`, `programs/marginfi/src/instructions/juplend/deposit.rs`)

### Summary
The Vader `mintFungible` bug allowed frontrunners to devalue a pool's reserves immediately before an LP's deposit, causing the LP to receive far fewer liquidity units than intended, because the mint function accepted no user-specified minimum output. marginfi-v2's integration deposit instructions (`kamino_deposit`, `juplend_deposit`, and the analogous `drift`/`solend` deposit flows) have the same structural weakness: the number of collateral tokens or shares an unprivileged user receives for a fixed input `amount` is derived from a live, externally-controlled exchange rate at execution time, and the instruction exposes no user-supplied minimum-output parameter to bound acceptable slippage.

### Finding Description
In `kamino_deposit`, the expected collateral for the deposit is computed from the *live* Kamino reserve ratio immediately before the CPI: [1](#0-0) 

The check afterward (`assert_within_one_token`) only verifies that the CPI's actual output matches the *just-computed* expected value — it does not protect the user against that live exchange rate itself having been shifted unfavorably (e.g., via a same-slot/adjacent transaction that alters `available_amount` / `mint_total_supply` on the underlying Kamino reserve, a classic vault "donation"/inflation attack, or ordinary interest-driven drift between signing and landing). There is no `min_collateral_out` (or equivalent) argument in `KaminoDeposit`'s instruction args (`amount: u64, refresh_reserve: Option<bool>`) for the caller to enforce a floor on what they will accept.

The same pattern exists in the JupLend deposit flow: `expected_shares` is computed from the current `lending.liquidity_exchange_price` / `token_exchange_price` right before the CPI, and the instruction only asserts the CPI outcome equals that just-computed expectation — again with no user-supplied minimum: [2](#0-1) 

Notably, the underlying JupLend program itself *does* expose a `deposit_with_min_amount_out` instruction with a `min_amount_out: u64` argument that JupLend integrators are expected to use for slippage protection: [3](#0-2) 
but marginfi's `juplend_deposit` CPIs into the plain `deposit` instruction instead, discarding this protection entirely and leaving the end user with no way to bound the shares they will accept.

This is a direct structural analog to the Vader `mintFungible` issue: value received by an unprivileged depositor is a function of a manipulable "pool" state (external reserve/exchange price) evaluated at execution time, with no caller-specified minimum acceptable output.

### Impact Explanation
An attacker who can shift the underlying Kamino/JupLend reserve ratio between when a victim signs a marginfi deposit transaction and when it lands (e.g., by depositing/donating/withdrawing large amounts on the shared external market, or simply by transaction ordering/latency in normal market conditions) can cause the victim to receive materially fewer collateral tokens/shares than the fair-value amount for their deposited liquidity. Because marginfi tracks the user's bank balance directly from the collateral/shares actually minted by the CPI (`bank_account.deposit_no_repay(...)`), any shortfall is permanently booked as the user's asset balance — a direct, unrecoverable value loss for the depositor, i.e. concrete theft of value analogous to the referenced Vader finding.

### Likelihood Explanation
This affects the unprivileged, permissionless deposit path (`kamino_deposit`, `juplend_deposit`, and equivalent `drift`/`solend` deposit instructions) that any user can call with only a signed marginfi account and their own token balance — no privileged role is required. The exchange rates in question (`collateral_to_liquidity`/`liquidity_to_collateral`, `token_exchange_price`) are shared, externally-driven market state that is not exclusively controlled by marginfi, making them plausible targets for manipulation or simply subject to natural drift under latency/reordering, without requiring any bug in marginfi's own core accounting.

### Recommendation
Add an explicit user-specified minimum-output parameter (e.g., `min_collateral_amount` for Kamino, `min_shares` for JupLend) to each integration deposit instruction, and require the actually-minted collateral/shares to meet or exceed that minimum before crediting the user's balance and finalizing the transaction — mirroring the `deposit_with_min_amount_out` pattern already available in the underlying JupLend program, and analogous protections for Kamino/Drift/Solend where available.

### Proof of Concept
1. Victim submits `kamino_deposit(amount)` (or `juplend_deposit(amount)`) intending to receive collateral/shares priced at the current reserve exchange rate.
2. Before the transaction lands, an attacker shifts the shared external reserve's `available_amount`/`mint_total_supply` (Kamino) or `liquidity_exchange_price`/`token_exchange_price` (JupLend) unfavorably — e.g. via a large donation/withdraw sandwiching the victim's transaction on the underlying protocol.
3. `expected_collateral_amount`/`expected_shares` in `kamino_deposit`/`juplend_deposit` is computed against this now-unfavorable rate immediately before the CPI executes: [4](#0-3) 
4. The instruction has no parameter for the victim to reject this outcome; `bank_account.deposit_no_repay(...)` credits whatever collateral/shares resulted, permanently locking in the loss for the victim while the attacker (having reversed their manipulation) profits from the differential — the same value-extraction pattern described in the referenced Vader `mintFungible` finding.

### Citations

**File:** programs/marginfi/src/instructions/kamino/deposit.rs (L63-90)
```rust
    // Get initial obligation data to verify deposit amount later
    let initial_obligation_deposited_amount =
        ctx.accounts.integration_acc_2.load()?.deposits[0].deposited_amount;
    let expected_collateral_amount = ctx
        .accounts
        .integration_acc_1
        .load()?
        .liquidity_to_collateral(amount)?;

    if refresh_reserve {
        ctx.accounts.cpi_refresh_reserve()?;
    }

    ctx.accounts.cpi_transfer_user_to_obligation_owner(amount)?;
    ctx.accounts.cpi_kamino_deposit(amount, authority_bump)?;

    let final_obligation_deposited_amount =
        ctx.accounts.integration_acc_2.load()?.deposits[0].deposited_amount;

    // Verifying the deposit was successful by checking obligation balance increased by the correct amount
    let obligation_collateral_change =
        final_obligation_deposited_amount - initial_obligation_deposited_amount;
    assert_within_one_token(
        obligation_collateral_change,
        expected_collateral_amount,
        MarginfiError::KaminoDepositFailed,
    )?;

```

**File:** programs/marginfi/src/instructions/juplend/deposit.rs (L41-82)
```rust
pub fn juplend_deposit(ctx: Context<JuplendDeposit>, amount: u64) -> MarginfiResult {
    let authority_bump: u8;
    {
        let marginfi_account = ctx.accounts.marginfi_account.load()?;
        let bank = ctx.accounts.bank.load()?;
        authority_bump = bank.liquidity_vault_authority_bump;

        validate_asset_tags(&bank, &marginfi_account)?;
        validate_bank_state(&bank, InstructionKind::FailsIfPausedOrReduceState)?;
    }

    // Refresh the exchange price (interest/rewards) for this slot.
    ctx.accounts.cpi_update_rate()?;

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
```

**File:** idls-complete/juplend_earn.json (L97-209)
```json
      "args": [
        {
          "name": "assets",
          "type": "u64"
        }
      ],
      "returns": "u64"
    },
    {
      "name": "deposit_with_min_amount_out",
      "discriminator": [
        116,
        144,
        16,
        97,
        118,
        109,
        40,
        119
      ],
      "accounts": [
        {
          "name": "signer",
          "writable": true,
          "signer": true
        },
        {
          "name": "depositor_token_account",
          "writable": true
        },
        {
          "name": "recipient_token_account",
          "writable": true
        },
        {
          "name": "mint",
          "relations": [
            "lending",
            "rewards_rate_model"
          ]
        },
        {
          "name": "lending_admin"
        },
        {
          "name": "lending",
          "writable": true
        },
        {
          "name": "f_token_mint",
          "writable": true,
          "relations": [
            "lending"
          ]
        },
        {
          "name": "supply_token_reserves_liquidity",
          "writable": true
        },
        {
          "name": "lending_supply_position_on_liquidity",
          "writable": true
        },
        {
          "name": "rate_model"
        },
        {
          "name": "vault",
          "writable": true
        },
        {
          "name": "liquidity",
          "writable": true
        },
        {
          "name": "liquidity_program",
          "relations": [
            "lending_admin"
          ]
        },
        {
          "name": "rewards_rate_model"
        },
        {
          "name": "token_program"
        },
        {
          "name": "associated_token_program",
          "address": "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
        },
        {
          "name": "system_program",
          "address": "11111111111111111111111111111111"
        }
      ],
      "args": [
        {
          "name": "assets",
          "type": "u64"
        },
        {
          "name": "min_amount_out",
          "type": "u64"
        }
      ]
    },
    {
      "name": "init_lending",
      "discriminator": [
        156,
        224,
        67,
        46,
```
