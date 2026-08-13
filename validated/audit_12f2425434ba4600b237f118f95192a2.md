### Title
Unprivileged account-index squatting via unchecked `new_authority` in `transfer_to_new_account_pda` permanently blocks victims from creating/migrating PDA accounts - ([File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs])

### Summary
`transfer_to_new_account_pda` derives the destination PDA from `(group, new_authority, account_index, third_party_id)`, but `new_authority` is an `UncheckedAccount` that never signs the transaction. Any unprivileged caller can supply an arbitrary victim pubkey as `new_authority`, causing the program to permanently `init` the PDA slot that the victim would otherwise use for their own account, with no way for the victim to reclaim or reuse that `(account_index, third_party_id)` slot afterward. This is a direct analog of the external report's account-ID squatting/DoS bug class, and is strictly easier to exploit because it requires no front-running of a pending victim transaction — the attacker can squat any victim address proactively at any time.

### Finding Description
The PDA for a migrated account is derived purely from public, attacker-controllable inputs: [1](#0-0) 

`new_authority` is explicitly documented as unchecked:
```
/// CHECK: WARN: New authority is completely unchecked
pub new_authority: UncheckedAccount<'info>,
``` [2](#0-1) 

The only signer requirements are `authority` (the attacker's own old account owner) and `fee_payer` (also the attacker): [3](#0-2) 

The same seed derivation `(MARGINFI_ACCOUNT_SEED, group, authority/new_authority, account_index, third_party_id)` is reused both for a user's own direct PDA creation, `MarginfiAccountInitializePda`, and for the "gift" flow `TransferToNewAccountPda`: [4](#0-3) 

Because Anchor's `#[account(init, seeds = [...])]` constraint fails if the target PDA already exists, whichever party creates the PDA first "wins" that `(group, authority, account_index, third_party_id)` slot forever. Since `new_authority` in the transfer/migration path is never required to sign, an attacker can:
1. Create a disposable throwaway `MarginfiAccount` for themselves via the fully permissionless `marginfi_account_initialize`.
2. Call `transfer_to_new_account_pda` from that throwaway account, setting `new_authority` = the victim's public key and `account_index` = 0 (or any index the attacker wants to block).
3. Pay the flat `ACCOUNT_TRANSFER_FEE` (~$0.50) and rent.

This permanently occupies the victim's PDA slot for that `account_index`/`third_party_id`, since the resulting account's `authority` field is set to the victim's pubkey but the address itself is already initialized. Any subsequent attempt by the real victim to call `marginfi_account_initialize_pda` or `transfer_to_new_account_pda` targeting that same `(account_index, third_party_id)` will fail with an "already in use" / account-already-initialized error, exactly mirroring the report's account-ID squatting DoS, but without needing to observe or front-run any pending victim transaction — the attack works proactively against any address at any time.

### Impact Explanation
This is a permanent griefing/DoS vector against unprivileged users: an attacker can, for negligible cost, permanently deny any target address the ability to create or migrate to specific PDA-based `MarginfiAccount` slots (e.g., the commonly-used default `account_index = 0`) within a group. While it does not directly cause fund theft, it fits the "permanent lock/freeze" and "griefing" impact classes accepted by the rules, since the victim's expected account slot becomes permanently unusable and any funds later routed to that authority/index combination land in an account effectively pre-seeded/controlled by attacker-chosen initial state derived from the attacker's throwaway account (`migrated_from`, flags, etc., via `initialize_migrated_account`).

### Likelihood Explanation
High likelihood: exploitation requires only (a) creating a free/cheap throwaway keypair-based `MarginfiAccount`, and (b) paying the flat `ACCOUNT_TRANSFER_FEE`. No privileged role, no timing/front-running race, and no interaction with the victim is required. The attack can be executed against any known address (e.g., popular protocol-integration authorities) at will.

### Recommendation
Require `new_authority` to sign `TransferToNewAccountPda` (and any other instruction that derives a PDA using a caller-supplied pubkey as a seed on behalf of a third party), or otherwise validate that the destination authority has consented (e.g., via a pre-registered delegation/allow-list, similar to the `third_party_id` CPI-registration mechanism already used elsewhere in the codebase, see `is_allowed_cpi_for_third_party_id`): [5](#0-4) 

### Proof of Concept
1. Attacker calls `marginfi_account_initialize` (permissionless) to create a throwaway `MarginfiAccount` with themselves as authority — no deposits needed.
2. Attacker calls `transfer_to_new_account_pda(account_index = 0, third_party_id = None)` with:
   - `old_marginfi_account` = attacker's throwaway account
   - `authority` = attacker (signs)
   - `new_authority` = victim's pubkey (does **not** sign — `UncheckedAccount`)
   - pays `ACCOUNT_TRANSFER_FEE` via CPI transfer inside the handler.
3. The instruction succeeds, `init`-ing the PDA at `derive_pda(group, victim_pubkey, 0, None)`.
4. Victim later attempts `marginfi_account_initialize_pda(account_index = 0, third_party_id = None)` themselves — the transaction fails because the PDA already exists ("already in use"), as demonstrated by the existing negative test for duplicate PDA creation: [6](#0-5) 
5. The victim is permanently unable to use `account_index = 0` for their own PDA-based marginfi account in that group, without ever having signed or authorized the attacker's transaction.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L292-317)
```rust
    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>(),
        seeds = [
            MARGINFI_ACCOUNT_SEED.as_bytes(),
            group.key().as_ref(),
            new_authority.key().as_ref(),
            &account_index.to_le_bytes(),
            &third_party_id.unwrap_or(0).to_le_bytes(),
        ],
        bump
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/initialize.rs (L105-126)
```rust
#[derive(Accounts)]
#[instruction(account_index: u16, third_party_id: Option<u16>)]
pub struct MarginfiAccountInitializePda<'info> {
    #[account(
        constraint = !marginfi_group.load()?.is_protocol_paused() @ MarginfiError::ProtocolPaused
    )]
    pub marginfi_group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>(),
        seeds = [
            MARGINFI_ACCOUNT_SEED.as_bytes(),
            marginfi_group.key().as_ref(),
            authority.key().as_ref(),
            &account_index.to_le_bytes(),
            &third_party_id.unwrap_or(0).to_le_bytes(),
        ],
        bump
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,
```

**File:** programs/marginfi/src/constants.rs (L144-175)
```rust
pub fn is_allowed_cpi_for_third_party_id(
    sysvar_info: &AccountInfo,
    third_party_id: u16,
) -> MarginfiResult<bool> {
    // Free tier: no gating at all.
    if third_party_id < PDA_FREE_THRESHOLD {
        return Ok(true);
    }

    // Restricted tier: must have a rule.
    let allowed_program = match THIRD_PARTY_CPI_RULES
        .iter()
        .find(|(id, _)| *id == third_party_id)
        .map(|(_, program_id)| *program_id)
    {
        Some(p) => p,
        None => {
            return Ok(false);
        }
    };

    let current_ix_index = load_current_index_checked(sysvar_info)?;
    let current_ixn = load_instruction_at_checked(current_ix_index as usize, sysvar_info)?;

    // If the current (top-level) instruction is *this* program, it's a direct call (not CPI) -> no
    // "third party" id allowed in the restricted zone.
    if current_ixn.program_id == crate::ID {
        return Ok(false);
    }

    Ok(current_ixn.program_id == allowed_program)
}
```

**File:** programs/marginfi/tests/user_actions/create_account_pda.rs (L316-387)
```rust
#[tokio::test]
async fn marginfi_account_create_pda_duplicate_fails() -> anyhow::Result<()> {
    let test_f = TestFixture::new(None).await;

    let authority = test_f.payer();
    let account_index = 0;
    let third_party_id = None;

    // Derive PDA for the marginfi account
    let (marginfi_account_pda, _bump) = MarginfiAccount::derive_pda(
        &test_f.marginfi_group.key,
        &authority,
        account_index,
        third_party_id,
        &marginfi::ID,
    );

    let accounts = marginfi::accounts::MarginfiAccountInitializePda {
        marginfi_group: test_f.marginfi_group.key,
        marginfi_account: marginfi_account_pda,
        authority: authority,
        fee_payer: authority,
        instructions_sysvar: solana_instructions_sysvar::id(),
        system_program: system_program::id(),
    };

    let init_marginfi_account_pda_ix = Instruction {
        program_id: marginfi::ID,
        accounts: accounts.to_account_metas(Some(true)),
        data: marginfi::instruction::MarginfiAccountInitializePda {
            account_index,
            third_party_id,
        }
        .data(),
    };

    // First transaction should succeed
    let tx1 = Transaction::new_signed_with_payer(
        &[init_marginfi_account_pda_ix.clone()],
        Some(&test_f.payer()),
        &[&test_f.payer_keypair()],
        test_f.get_latest_blockhash().await,
    );

    let res1 = test_f
        .context
        .borrow_mut()
        .banks_client
        .process_transaction(tx1)
        .await;

    assert!(res1.is_ok());

    // Second transaction with same parameters should fail
    let tx2 = Transaction::new_signed_with_payer(
        &[init_marginfi_account_pda_ix],
        Some(&test_f.payer()),
        &[&test_f.payer_keypair()],
        test_f.get_latest_blockhash().await,
    );

    let res2 = test_f
        .context
        .borrow_mut()
        .banks_client
        .process_transaction(tx2)
        .await;

    assert!(res2.is_err(), "Duplicate account creation should fail");

    Ok(())
}
```
