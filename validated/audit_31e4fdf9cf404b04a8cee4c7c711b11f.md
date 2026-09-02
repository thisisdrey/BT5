[1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** contracts/defuse/src/contract/mod.rs (L72-87)
```rust
#[access_control(role_type(Role))]
#[derive(Pausable, PanicOnDefault)]
#[pausable(
    pause_roles(Role::DAO, Role::PauseManager),
    unpause_roles(Role::DAO, Role::UnpauseManager)
)]
#[near(
    contract_state,
    contract_metadata(
        standard(standard = "dip4", version = "0.1.0"),
        standard(standard = "nep245", version = "1.0.0"),
    )
)]
#[autoimpl(Deref using self.storage)]
#[autoimpl(DerefMut using self.storage)]
pub struct Contract {
```

**File:** contracts/defuse/src/contract/mod.rs (L120-131)
```rust
    pub fn new(config: DefuseConfig) -> Self {
        let mut contract = Self {
            storage: ContractStorage {
                accounts: Accounts::new(Prefix::Accounts),
                state: ContractState::new(Prefix::State, config.wnear_id, config.fees),
                relayer_keys: LookupSet::new(Prefix::RelayerKeys),
            },
            runtime: Runtime::default(),
        };
        contract.init_acl(config.roles);
        contract
    }
```

**File:** contracts/defuse/src/contract/intents/mod.rs (L44-64)
```rust
    #[pause(name = "intents")]
    fn simulate_intents(&self, signed: Vec<MultiPayload>) -> SimulationOutput {
        let mut inspector = SimulateInspector::default();
        let engine = Engine::new(self.cached(), &mut inspector);

        let invariant_violated = match engine.execute_signed_intents(signed) {
            // do not log transfers
            Ok(_) => None,
            Err(DefuseError::InvariantViolated(v)) => Some(v),
            Err(err) => err.panic(),
        };

        SimulationOutput {
            report: inspector.into_report(),
            invariant_violated,
            state: StateOutput {
                fee: self.fee(),
                current_salt: self.salts.current(),
            },
        }
    }
```
