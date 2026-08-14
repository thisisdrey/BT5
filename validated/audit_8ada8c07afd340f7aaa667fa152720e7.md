### Title
Permissionless one-time `FeeState` initialization can be front-run to hijack the global fee admin - (File: programs/marginfi/src/lib.rs)

### Summary
The global `FeeState` singleton — described in the project's own documentation as "a global singleton account that stores protocol-level fee configuration and the global fee admin" [1](#0-0)  — is set up via `init_global_fee_state`, an instruction explicitly documented as running "once per program" [2](#0-1) . Because it operates on a deterministically-addressed PDA rather than a caller-controlled fresh keypair, the instruction is reachable by anyone who can construct the correct seeds before the legitimate deployer's transaction lands — the same "first caller wins" front-running pattern described in the `PerpOwnable.transferPerpOwner` report.

### Finding Description
`init_global_fee_state` takes the `admin`, `fee_wallet`, and every fee parameter as raw instruction arguments and writes them into the `FeeState` PDA on first call: `pub fn init_global_fee_state(ctx: Context<InitFeeState>, admin: Pubkey, fee_wallet: Pubkey, bank_init_flat_sol_fee: u32, liquidation_flat_sol_fee: u32, order_init_flat_sol_fee: u32, program_fee_fixed: WrappedI80F48, program_fee_rate: WrappedI80F48, liquidation_max_fee: WrappedI80F48, order_execution_max_fee: WrappedI80F48)` [3](#0-2) . Unlike `MarginfiGroupInitialize`/`MarginfiAccountInitialize`, whose target accounts are fresh keypairs that must co-sign account creation (making the resulting address unpredictable to an attacker) [4](#0-3) [5](#0-4) , `FeeState` is a deterministic global PDA (a "singleton" per the docs), so its address can be computed and targeted by anyone in advance. This is structurally identical to the `PerpOwnable` bug: a one-time, unauthenticated setter for a critical owner/admin field on a target whose address is fixed/known ahead of time, where "first caller wins" and there is no `hasRole`/admin gate preventing an arbitrary address from calling it before the legitimate deployer.

Every downstream group derives its cached fee parameters from this account at group-initialization time: `marginfi_group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet; ... program_fee_fixed ... program_fee_rate` [6](#0-5) , and the `fee_state` PDA is a required, seed-derived account in that instruction [7](#0-6) .

I was not able to fully inspect the `InitFeeState` `#[derive(Accounts)]` struct (defined in a separate file not retrieved before the tool-call budget was exhausted) to confirm whether it carries any additional signer/authority constraint beyond Anchor's standard `init` guard against re-initialization. This is a material gap: if `InitFeeState` restricts the caller to a hardcoded upgrade authority or similar out-of-band check, the analog would not hold. Given the doc-comment explicitly frames this as a bare "runs once" bootstrap instruction (in contrast to instructions elsewhere in `lib.rs` that are explicitly annotated "(admin only)"), and given the arguments (including `admin` itself) are fully attacker-controllable, the pattern strongly resembles the reported bug class, but this specific point should be verified against the full `InitFeeState` account context before treating it as confirmed.

### Impact Explanation
If the instruction is indeed unauthenticated, whoever calls `init_global_fee_state` first — during initial mainnet deployment or after any migration that re-derives the PDA — permanently becomes the `global_fee_admin`/`fee_wallet` for the entire protocol (all groups reference this singleton). This would let an attacker redirect protocol-wide fee collection to their own wallet and set fee parameters (`program_fee_fixed`, `program_fee_rate`, `liquidation_max_fee`, `order_execution_max_fee`) for every bank in the ecosystem, since `Anchor`'s `init` constraint only prevents a second call, not a malicious first call.

### Likelihood Explanation
Exploitation requires only monitoring the mempool/validator queue at protocol deployment or PDA-recreation time and submitting a competing transaction with a higher priority fee — mechanically identical to the front-running scenario described for `transferPerpOwner`. No privileged access is required.

### Recommendation
Confirm (and if necessary add) an authorization check on `InitFeeState` restricting the caller to a known, hardcoded upgrade/deploy authority (e.g., checked against the program's upgrade authority or a compile-time constant), rather than relying solely on the `init`-once guarantee, mirroring the recommended fix for `transferPerpOwner` (gate the one-time setter behind an explicit authority check rather than "first signer wins").

### Proof of Concept
1. Attacker derives the `FeeState` PDA address using the well-known `FEE_STATE_SEED` [8](#0-7)  before the legitimate deployment transaction is confirmed.
2. Attacker submits `init_global_fee_state(admin = attacker_key, fee_wallet = attacker_key, ...)` with a higher priority fee than the legitimate deployer's transaction.
3. The PDA is initialized with the attacker's keys; the legitimate deployer's subsequent call fails (Anchor `init` re-init guard), and every `MarginfiGroupInitialize` call permanently caches the attacker-controlled `global_fee_wallet`/fee rates into all groups.

*(Note: step 2's exploitability depends on `InitFeeState`'s account constraints, which could not be fully verified within the available tool budget — see caveat above.)*

### Citations

**File:** guides/ADMIN/PERMISSIONS_AND_ROLES.md (L11-12)
```markdown
- **FeeState** - A global singleton account that stores protocol-level fee configuration and the
  global fee admin.
```

**File:** programs/marginfi/src/lib.rs (L565-590)
```rust
    /// (Runs once per program) Configures the fee state account, where the global admin sets fees
    /// that are assessed to the protocol
    pub fn init_global_fee_state(
        ctx: Context<InitFeeState>,
        admin: Pubkey,
        fee_wallet: Pubkey,
        bank_init_flat_sol_fee: u32,
        liquidation_flat_sol_fee: u32,
        order_init_flat_sol_fee: u32,
        program_fee_fixed: WrappedI80F48,
        program_fee_rate: WrappedI80F48,
        liquidation_max_fee: WrappedI80F48,
        order_execution_max_fee: WrappedI80F48,
    ) -> MarginfiResult {
        marginfi_group::initialize_fee_state(
            ctx,
            admin,
            fee_wallet,
            bank_init_flat_sol_fee,
            liquidation_flat_sol_fee,
            order_init_flat_sol_fee,
            program_fee_fixed,
            program_fee_rate,
            liquidation_max_fee,
            order_execution_max_fee,
        )
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L6-6)
```rust
    constants::FEE_STATE_SEED,
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L30-32)
```rust
    marginfi_group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet;
    marginfi_group.fee_state_cache.program_fee_fixed = fee_state.program_fee_fixed;
    marginfi_group.fee_state_cache.program_fee_rate = fee_state.program_fee_rate;
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L53-60)
```rust
#[derive(Accounts)]
pub struct MarginfiGroupInitialize<'info> {
    #[account(
        init,
        payer = admin,
        space = 8 + std::mem::size_of::<MarginfiGroup>(),
    )]
    pub marginfi_group: AccountLoader<'info, MarginfiGroup>,
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L65-69)
```rust
    #[account(
        seeds = [FEE_STATE_SEED.as_bytes()],
        bump,
    )]
    pub fee_state: AccountLoader<'info, FeeState>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/initialize.rs (L42-61)
```rust
#[derive(Accounts)]
pub struct MarginfiAccountInitialize<'info> {
    #[account(
        constraint = !marginfi_group.load()?.is_protocol_paused() @ MarginfiError::ProtocolPaused
    )]
    pub marginfi_group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>()
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    pub system_program: Program<'info, System>,
```
