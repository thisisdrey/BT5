[1](#0-0)  confirms that `emissions_destination_account` is unconditionally copied from the old account to the freshly-initialized new account during authority transfer, with no clearing or re-confirmation logic, closely mirroring the "seller-configured state silently surviving an ownership hand-off" bug class from the report.

### Title
`transfer_to_new_account`/`transfer_to_new_account_pda` silently copy `emissions_destination_account`, letting a seller permanently redirect a buyer's future emissions to themselves - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
`MarginfiAccount::emissions_destination_account` is a user-settable field via `marginfi_account_update_emissions_destination_account` that determines where off-chain emissions/incentive airdrops are delivered. When an account authority is transferred via `transfer_to_new_account` or `transfer_to_new_account_pda`, `initialize_migrated_account` copies this field verbatim from the old account into the newly created account, which is otherwise freshly initialized for the `new_authority`. The new owner never explicitly consents to or is prompted about this inherited value, so a seller can pre-set `emissions_destination_account` to their own wallet before transferring/selling the account, causing the buyer's future emissions to be diverted to the seller indefinitely, until the buyer notices and manually overwrites the field.

### Finding Description
`marginfi_account_update_emissions_destination_account` lets the current authority freely set `emissions_destination_account` to any pubkey: [2](#0-1) 

When the account authority later performs `transfer_to_new_account` (or the PDA variant), a brand-new `MarginfiAccount` is created for `new_authority`, but `initialize_migrated_account` explicitly copies over `lending_account`, `emissions_destination_account`, and `account_flags` from the old account: [1](#0-0) 

The `TransferToNewAccount` accounts struct shows `new_authority` is a completely unchecked account and does not need to sign or approve anything about the resulting account's configuration: [3](#0-2) 

Documentation for the instruction only warns that emissions "will still airdrop to the old account for that week" for book-keeping — it does not disclose that `emissions_destination_account` itself is copied forward into the new account and will continue redirecting all future weekly airdrops unless the new owner discovers and resets it: [4](#0-3) 

This is the direct analog of the reported bug class: a previous account controller installs persistent configuration (in the OCL case, a delegatecall fallback module; here, an emissions redirect address) that is not cleared on an ownership-transfer instruction, and the new/incoming owner inherits it with no on-chain signal that anything unusual was configured. There is a `// TODO emissions destination and/or flags?` comment in the TypeScript test suite acknowledging this gap was never resolved: [5](#0-4) 

### Impact Explanation
Any user who acquires a marginfi account via `transfer_to_new_account`/`transfer_to_new_account_pda` (e.g., through an account-sale/migration flow, or an integrator moving a user to a PDA-based account) can have every future weekly emissions/incentive airdrop silently redirected to the address chosen by the previous authority, for as long as the new owner doesn't notice and doesn't manually call `marginfi_account_update_emissions_destination_account`. This is an unauthorized transfer of value (token incentive payouts) away from the rightful new owner to a party who no longer controls the account, with no integrity signal exposed to the recipient.

### Likelihood Explanation
The path is fully reachable by unprivileged users: any account authority can call `marginfi_account_update_emissions_destination_account` before initiating `transfer_to_new_account`/`transfer_to_new_account_pda`, both of which are ordinary instructions available to any account holder. No admin or validator privilege is required, and the "sale/transfer" flow is an explicitly supported, documented feature of the protocol.

### Recommendation
Reset `emissions_destination_account` to `Pubkey::default()` (or to `new_authority`) in `initialize_migrated_account` whenever the authority changes, rather than copying the previous authority's configured destination. Alternatively, require the `new_authority` to explicitly re-confirm or set the emissions destination as part of the transfer instruction, and surface a clear on-chain/off-chain signal to the new owner that this field was inherited from the prior owner.

### Proof of Concept
1. User A creates a marginfi account and calls `marginfi_account_update_emissions_destination_account` setting `destination_account = A_wallet` [6](#0-5) .
2. User A transfers/sells the account to User B via `transfer_to_new_account`, setting `new_authority = B_wallet`.
3. `initialize_migrated_account` copies `emissions_destination_account = A_wallet` into the new account owned by B [1](#0-0) .
4. User B now owns and operates the account (deposits/borrows), earning emissions campaigns, but every weekly airdrop continues to be delivered to `A_wallet` until B independently discovers and overwrites `emissions_destination_account`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L23-37)
```rust
fn initialize_migrated_account(
    new_account: &mut MarginfiAccount,
    old_account: &MarginfiAccount,
    new_authority: Pubkey,
    current_timestamp: u64,
    old_account_key: Pubkey,
) {
    new_account.initialize(old_account.group, new_authority, current_timestamp);
    new_account.lending_account = old_account.lending_account;
    new_account.emissions_destination_account = old_account.emissions_destination_account;
    new_account.account_flags = old_account.account_flags;
    new_account.migrated_from = old_account_key;
    new_account.indexer_flags = old_account.indexer_flags;
    new_account.sync_indexer_flags();
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L146-166)
```rust
    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>()
    )]
    pub new_marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,

    /// CHECK: Validated against group fee state cache
    #[account(mut)]
    pub global_fee_wallet: UncheckedAccount<'info>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/emissions.rs (L10-24)
```rust
/// (account authority) Set the wallet whose canonical ATA will receive
/// off-chain emissions distributions.
pub fn marginfi_account_update_emissions_destination_account(
    ctx: Context<MarginfiAccountUpdateEmissionsDestinationAccount>,
) -> MarginfiResult {
    let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;

    check!(
        !marginfi_account.get_flag(ACCOUNT_FROZEN),
        MarginfiError::AccountFrozen
    );

    marginfi_account.emissions_destination_account = ctx.accounts.destination_account.key();
    Ok(())
}
```

**File:** guides/DEVELOPERS_INTEGRATORS/GETTING_STARTED_INTEGRATOR.md (L118-131)
```markdown
<details>
<summary> <b>marginfi_account_update_emissions_destination_account</b> - set an emissions destination</summary>

- Highly encouraged if the Account is owned by a PDA. All emissions will be sent here instead of
to the authority.
</details>

<details>
<summary> <b>transfer_to_new_account/transfer_to_new_account_pda</b> - Move to a new authority</summary>

- Points earned will (eventually) go to the new account/authority, but you will still see points on
the old account for book-keeping reasons, and emissions will still airdrop to the old account for
that week.
</details>
```

**File:** tests/specs/basic/12_transfer_account.spec.ts (L270-270)
```typescript
  // TODO emissions destination and/or flags?
```
