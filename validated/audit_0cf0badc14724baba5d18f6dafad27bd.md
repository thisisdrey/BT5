## Finding

### Title
Permissionless, unauthenticated global fee-state initialization allows front-running to hijack protocol admin — (File: `programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs`)

### Summary
The instruction that initializes the program-wide `FeeState` singleton PDA can be called by anyone, with no check that the caller is the intended protocol deployer/admin. Because the account is a PDA derived solely from a fixed seed (`FEE_STATE_SEED`), it can only ever be initialized once; whoever calls it first permanently becomes the `global_fee_admin` and sets the `global_fee_wallet` that receives protocol fees.

### Finding Description
`initialize_fee_state` sets `fee_state.global_fee_admin` and `fee_state.global_fee_wallet` directly from caller-supplied arguments (`admin_key`, `fee_wallet`), with the only signer requirement being an arbitrary `payer: Signer` that pays rent — there is no `has_one`, `constraint`, or hardcoded-pubkey check tying the call to the real deployer: [1](#0-0) 

The account struct only requires `payer` and the `fee_state` PDA (seeded by the fixed `FEE_STATE_SEED`), with no admin/owner gating on who can invoke it: [2](#0-1) 

Since the PDA has a single, deterministic seed and uses `init` (which fails once the account exists), this instruction can only succeed once globally. This is the on-chain analog of the reported `Staking.sol` bug class: an `initialize()`-style function reachable by any unprivileged caller that permanently fixes security-critical state (there, a staking start timestamp; here, the entire program's fee-admin authority and fee-destination wallet).

This matches the entrypoint exposed in `lib.rs` with no additional gating at the dispatch layer either.

### Impact Explanation
Whoever wins the race to call this instruction (e.g., by front-running the legitimate deployment transaction before the real admin's `init_global_fee_state` call lands) becomes `global_fee_admin` for the lifetime of the program (the PDA can never be reinitialized). The `global_fee_admin` role controls security-relevant, protocol-wide levers, including:
- `edit_global_fee` — which reads/writes `global_fee_admin` and fee configuration [3](#0-2) 
- `config_group_fee`, which references `global_fee_admin` [4](#0-3) 
- `panic_unpause`, a global pause/unpause authority tied to `global_fee_admin` [5](#0-4) 

An attacker who seizes this role can redirect all protocol fee revenue to a wallet they control (`global_fee_wallet`) and/or gain unauthorized control over protocol-wide pause/fee configuration — this is a concrete unauthorized state change and theft-of-fee-revenue risk, not merely theoretical, since it permanently locks out the legitimate admin from the singleton PDA.

### Likelihood Explanation
Exploitation requires winning a single race against the legitimate deployer's initialization transaction (classic front-running/init-race on Solana, since instructions and account derivations are public before confirmation, e.g. visible in the mempool/RPC prior to landing). This is the same bug class as the reported `Staking.sol` issue and is directly reachable without any privileged role, satisfying the "unprivileged-user analog" requirement. I was not able to fully verify from the index whether any deployment tooling atomically bundles this call with restrictions (e.g., a single combined init transaction) that would mitigate the race in practice — this should be confirmed against actual deployment scripts/CI.

### Recommendation
Restrict `InitFeeState`/`initialize_fee_state` to a known, hardcoded upgrade authority or program-derived deployer key (e.g., verify `payer.key()` against the program's upgrade authority, or require a signature from a fixed, pre-agreed admin pubkey), analogous to adding an `onlyOwner`-style constraint. Alternatively, bundle fee-state initialization atomically within the same transaction as program deployment/upgrade so no separate, front-runnable transaction exists.

### Proof of Concept
1. Deploy program (or observe it deployed) before the legitimate admin's `init_global_fee_state` transaction lands.
2. Derive the `fee_state` PDA via `Pubkey::find_program_address(&[FEE_STATE_SEED.as_bytes()], &marginfi::ID)` (deterministic, publicly derivable).
3. Submit `marginfi::instruction::InitFeeState` (or the corresponding client call) with `admin_key = attacker_pubkey`, `fee_wallet = attacker_wallet`, and `payer = attacker`, before the legitimate admin's transaction confirms.
4. Because `fee_state` uses Anchor `init` on a fixed-seed PDA, the legitimate admin's subsequent identical transaction fails (`already in use`), permanently locking `global_fee_admin` and `global_fee_wallet` to the attacker's chosen values as shown in `initialize_fee_state`: [6](#0-5)

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L8-35)
```rust
#[allow(unused_variables)]
pub fn initialize_fee_state(
    ctx: Context<InitFeeState>,
    admin_key: Pubkey,
    fee_wallet: Pubkey,
    bank_init_flat_sol_fee: u32,
    liquidation_flat_sol_fee: u32,
    order_init_flat_sol_fee: u32,
    program_fee_fixed: WrappedI80F48,
    program_fee_rate: WrappedI80F48,
    liquidation_max_fee: WrappedI80F48,
    order_execution_max_fee: WrappedI80F48,
) -> Result<()> {
    let mut fee_state = ctx.accounts.fee_state.load_init()?;
    fee_state.global_fee_admin = admin_key;
    fee_state.global_fee_wallet = fee_wallet;
    fee_state.key = ctx.accounts.fee_state.key();
    fee_state.bank_init_flat_sol_fee = bank_init_flat_sol_fee;
    fee_state.bump_seed = ctx.bumps.fee_state;
    fee_state.program_fee_fixed = program_fee_fixed;
    fee_state.program_fee_rate = program_fee_rate;
    fee_state.liquidation_max_fee = liquidation_max_fee;
    fee_state.liquidation_flat_sol_fee = liquidation_flat_sol_fee;
    fee_state.order_execution_max_fee = order_execution_max_fee;
    fee_state.order_init_flat_sol_fee = order_init_flat_sol_fee;

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/init_global_fee_state.rs (L37-55)
```rust
#[derive(Accounts)]
pub struct InitFeeState<'info> {
    /// Pays the init fee
    #[account(mut)]
    pub payer: Signer<'info>,

    #[account(
        init,
        seeds = [
            FEE_STATE_SEED.as_bytes()
        ],
        bump,
        payer = payer,
        space = 8 + FeeState::LEN,
    )]
    pub fee_state: AccountLoader<'info, FeeState>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs (L1-1)
```rust
// Global fee admin calls this to edit fee state fields (all optional).
```

**File:** programs/marginfi/src/instructions/marginfi_group/config_group_fee.rs (L1-1)
```rust
use crate::{state::marginfi_group::MarginfiGroupImpl, MarginfiError, MarginfiResult};
```

**File:** programs/marginfi/src/instructions/marginfi_group/panic_unpause.rs (L1-1)
```rust
use anchor_lang::prelude::*;
```
