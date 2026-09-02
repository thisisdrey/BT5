No vulnerability found for this question.

This codebase (NEAR Intents) has no analog to the Beefy `StrategyPassiveManagerUniswap` bug class. The vulnerability hinges on a concentrated-liquidity AMM strategy that reads the pool's current tick (`pool.slot0`) and redeploys liquidity into a price range whenever an owner calls `setPositionWidth`/`unpause`, without a `onlyCalmPeriods` check — allowing an attacker to manipulate the spot price immediately before the owner's transaction lands.

In-scope contracts (`contracts/defuse/**`, `contracts/escrow-swap/**` is excluded, `contracts/wallet/**`, `contracts/poa/**`, `contracts/global-deployer/src/**`, `contracts/outlayer/app/src/**`, `contracts/treasury-logger/src/**`) do not implement any AMM, tick-based liquidity position, or price-range redeployment logic. The `defuse` verifier settles pre-signed `MultiPayload` intents via `Engine::execute_signed_intents` [1](#0-0) , and pause/unpause on that contract is gated behind `Role::PauseManager`/`Role::UnpauseManager` with no re-pricing side effects [2](#0-1) . The PoA factory similarly only gates deploy/deposit operations behind roles, with no price-sensitive state redeployment [3](#0-2) . There is no code path where an unprivileged attacker can manipulate an on-chain price oracle/pool state that a subsequent privileged call would then use to reconfigure custody of funds into an unfavorable range — the underlying "calm period"/tick-manipulation bug class simply does not exist in this repository's scope.

### Citations

**File:** contracts/defuse/src/contract/intents/mod.rs (L24-42)
```rust
#[near]
impl Intents for Contract {
    #[pause(name = "intents")]
    fn execute_intents(&mut self, signed: Vec<MultiPayload>) {
        if let Some(event) = Engine::new(self, ExecuteInspector::default())
            .execute_signed_intents(signed)
            .unwrap_or_else(|e| e.panic())
            .as_mt_event()
        {
            // NOTE: Not all `mt_transfer` events are refundable, but it's safe to check them
            // all at once since non-refundable transfers only increase the potential refund
            // log size without affecting correctness. This can actually prevent resolve transfer
            // from failing due to too long event log !!!
            event
                .check_refund()
                .unwrap_or_else(|err| err.panic())
                .emit();
        }
    }
```

**File:** contracts/defuse/src/contract/mod.rs (L72-77)
```rust
#[access_control(role_type(Role))]
#[derive(Pausable, PanicOnDefault)]
#[pausable(
    pause_roles(Role::DAO, Role::PauseManager),
    unpause_roles(Role::DAO, Role::UnpauseManager)
)]
```

**File:** contracts/poa/factory/src/contract.rs (L54-60)
```rust
#[near(contract_state, contract_metadata())]
#[derive(Pausable, PanicOnDefault)]
#[access_control(role_type(Role))]
#[pausable(
    pause_roles(Role::DAO, Role::PauseManager),
    unpause_roles(Role::DAO, Role::UnpauseManager)
)]
```
