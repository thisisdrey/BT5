## Finding: `TransferToNewAccount`/`TransferToNewAccountPda` accept `new_authority == Pubkey::default()`, permanently freezing all migrated funds

### Title
Unvalidated zero-address `new_authority` in account migration permanently locks all transferred lending positions - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The `transfer_to_new_account` and `transfer_to_new_account_pda` instructions let a marginfi account's authority migrate all lending positions to a brand-new `MarginfiAccount` under an arbitrary `new_authority` pubkey, with **no check that `new_authority != Pubkey::default()`** on-chain, mirroring the reported ZeroLocker/FeeDistributor `_transferFrom` bug class where a transfer to the zero address is never rejected despite documentation/comments implying it should be restricted.

### Finding Description
`TransferToNewAccount` declares `new_authority` as a completely unchecked account: [1](#0-0) 

The instruction handler copies the entire `lending_account` (all deposit/borrow positions) from the old account into the freshly initialized `new_account`, sets `new_account.authority = new_authority`, and disables the old account — with zero validation on `new_authority`: [2](#0-1) [3](#0-2) 

The only checks performed are for flashloan/receivership/order-execution flags, active orders, and prevention of double migration — none of these touch `new_authority`: [4](#0-3) 

The identical unchecked pattern exists in the PDA variant: [5](#0-4) 

Every downstream instruction (deposit/withdraw/borrow/repay/close) requires `authority: Signer<'info>` to match `account.authority`. `Pubkey::default()` (the all-zero pubkey) has no corresponding private key, so once `new_authority = Pubkey::default()` is set, the resulting `new_marginfi_account` — now holding all the migrated collateral/debt positions — can never be signed for again. This is functionally identical to the reported bug class: the zero-address destination silently "succeeds," state is fully updated (ownership/authority fields, balances), but the assets become permanently unreachable, with no ending checkpoint/burn semantics to protect them.

Awareness of this exact risk already exists in the codebase, but the safeguard is client-side only, not enforced on-chain:
- The CLI helper rejects it before ever building the instruction: [6](#0-5) 
- The TS integration test suite explicitly documents the lack of on-chain restriction: "WARN: User picks the new authority with no restrictions!" [7](#0-6) 

Because `transfer_to_new_account_pda` is explicitly designed for third-party/integrator CPI use (per the seeds documentation: "mostly this use-case applies to integrators that use accounts for whatever use-case"), a buggy or malicious integrator program can pass `new_authority = Pubkey::default()` on behalf of a user, permanently trapping that user's entire position: [8](#0-7) 

### Impact Explanation
Any account authority (or any CPI caller a user has authorized/interacts with in the third-party integration flow) can cause **all of a user's collateral and open borrow/lend positions to be permanently and irrecoverably locked** by migrating to a new account whose authority is the zero pubkey. Unlike a normal account close/withdraw, no funds are returned — the position data is fully copied into an account nobody can ever sign for, satisfying the "permanent lock/freeze" impact category. This is analogous to the reported ZeroLocker issue where the underlying asset becomes "stuck forever" in the vault contract once a zero-address transfer succeeds without a revert.

### Likelihood Explanation
The path is reachable by any unprivileged account authority calling a permissionless, documented instruction (`transfer_to_new_account`/`transfer_to_new_account_pda`); no admin privilege is required. It requires either user error/UI bug or a malicious/buggy third-party integrator supplying the zero pubkey as `new_authority` in the PDA flow that is explicitly designed for third-party CPI usage. This is a plausible, low-complexity trigger (a single instruction call with an unchecked account field), not merely theoretical, since the codebase's own client and test code demonstrate awareness of the gap without an on-chain fix.

### Recommendation
Add an on-chain check in both `transfer_to_new_account` and `transfer_to_new_account_pda` that rejects `new_authority == Pubkey::default()` (and any other reasonable evaluation of "no owner," e.g. matching a system-owned zero-lamport account), returning an explicit error (e.g. `MarginfiError::InvalidAuthority`) before any state mutation occurs, consistent with the existing off-chain guard in `p0-cli`: [9](#0-8) 

### Proof of Concept
1. User (or a third-party integrator program acting via `transfer_to_new_account_pda`) calls `transfer_to_new_account` on their own `MarginfiAccount` with all real positions (deposits/borrows) as in `programs/marginfi/tests/admin_actions/account_transfer.rs`, but supplies `new_authority = Pubkey::default()` instead of a real wallet key.
2. The instruction succeeds: `old_account` is disabled/zeroed, `new_account.authority` is set to `Pubkey::default()`, and the full `lending_account` (assets and liabilities) is copied into `new_account`, per `initialize_migrated_account`/`finalize_migrated_old_account`. [3](#0-2) 
3. Any subsequent attempt to operate on `new_account` (withdraw, repay, close, etc.) requires a `Signer` matching `authority == Pubkey::default()`, which is unsatisfiable since no private key exists for the zero pubkey — the migrated funds are permanently frozen with no recovery path.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L23-49)
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

fn finalize_migrated_old_account(
    old_account: &mut MarginfiAccount,
    new_account_key: Pubkey,
    current_timestamp: u64,
) {
    old_account.migrated_to = new_account_key;
    old_account.last_update = current_timestamp;
    old_account.lending_account = LendingAccount::zeroed();
    old_account.set_flag(ACCOUNT_DISABLED, true);
    old_account.sync_indexer_flags();
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L63-89)
```rust
    check!(
        !old_account.get_flag(ACCOUNT_IN_FLASHLOAN),
        MarginfiError::AccountInFlashloan
    );

    check!(
        !old_account.get_flag(ACCOUNT_IN_RECEIVERSHIP),
        MarginfiError::ForbiddenIx
    );

    check!(
        !old_account.get_flag(ACCOUNT_IN_ORDER_EXECUTION),
        MarginfiError::ForbiddenIx
    );

    check!(
        old_account.active_orders == 0,
        MarginfiError::IllegalAction,
        "Close all active orders before transfer"
    );

    // Prevent multiple migrations from the same account
    check_eq!(
        old_account.migrated_to,
        Pubkey::default(),
        MarginfiError::AccountAlreadyMigrated
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L91-105)
```rust
    let mut new_account = ctx.accounts.new_marginfi_account.load_init()?;
    let current_timestamp = Clock::get()?.unix_timestamp as u64;
    initialize_migrated_account(
        &mut new_account,
        &old_account,
        ctx.accounts.new_authority.key(),
        current_timestamp,
        ctx.accounts.old_marginfi_account.key(),
    );

    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L158-159)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L312-313)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** p0-cli/src/processor/account.rs (L1174-1181)
```rust
pub fn marginfi_account_transfer(
    profile: &Profile,
    config: &Config,
    new_authority: Pubkey,
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```

**File:** tests/specs/basic/12_transfer_account.spec.ts (L55-56)
```typescript
  // Here the user moves authority to some new wallet. WARN: User picks the new authority with no
  // restrictions!
```

**File:** programs/marginfi/src/lib.rs (L502-516)
```rust
    /// (account authority) Same as `transfer_to_new_account` except the resulting account is a PDA
    ///
    /// seeds:
    /// - marginfi_group
    /// - authority: The account authority (owner)  
    /// - account_index: A u16 value to allow multiple accounts per authority
    /// - third_party_id: Optional u16 for third-party tagging. Seeds < PDA_FREE_THRESHOLD can be
    ///   used freely. For a dedicated seed used by just your program (via CPI), contact us.
    pub fn transfer_to_new_account_pda(
        ctx: Context<TransferToNewAccountPda>,
        account_index: u16,
        third_party_id: Option<u16>,
    ) -> MarginfiResult {
        marginfi_account::transfer_to_new_account_pda(ctx, account_index, third_party_id)
    }
```
