No vulnerability found for this question.

The reported bug concerns a yield-bearing vault contract that computes `getAccruedYield` from strategy balances versus minted shares without accounting for asset de-pegging — a Solidity vault/strategy pattern with price oracles. The in-scope NEAR contracts (`contracts/defuse/**`, `contracts/wallet/**`, `contracts/poa/**`, `contracts/global-deployer/src/**`, `contracts/outlayer/app/src/**`, `contracts/treasury-logger/src/**`, `crates/**`) contain no vault, strategy, yield-accrual, or price-oracle logic to map this bug class onto. The `defuse` contract tracks token balances/supplies directly via mint/deposit/withdraw flows [1](#0-0) , `poa` is a plain fungible-token factory/token pair with no yield concept [2](#0-1) , and `treasury-logger` is a passive write-only event logger with no balance or price accounting at all [3](#0-2) . There is no equality binding (balance vs. signature, settlement count, batch delta sum, debit vs. delivered value, `TokenId` vs. asset moved, or fees owed vs. collected) that this report's under-peg yield-miscalculation class could break in this codebase.

### Citations

**File:** contracts/defuse/src/contract/tokens/mod.rs (L18-65)
```rust
    pub(crate) fn deposit(
        &mut self,
        owner_id: AccountId,
        tokens: impl IntoIterator<Item = (TokenId, u128)>,
        memo: Option<&str>,
    ) -> Result<()> {
        let owner = self
            .storage
            .accounts
            .get_or_create(owner_id.clone())
            // deposits are allowed for locked accounts
            .as_inner_unchecked_mut();

        let mut mint_event = MtMintEvent {
            owner_id: owner_id.into(),
            token_ids: Vec::new().into(),
            amounts: Vec::new().into(),
            memo: memo.map(Into::into),
        };

        for (token_id, amount) in tokens {
            if amount == 0 {
                return Err(DefuseError::InvalidIntent);
            }

            mint_event.token_ids.to_mut().push(token_id.to_string());
            mint_event.amounts.to_mut().push(amount);

            let total_supply = self
                .storage
                .state
                .total_supplies
                .add(token_id.clone(), amount)
                .ok_or(DefuseError::BalanceOverflow)?;
            match token_id {
                TokenId::Nep171(ref tid) => {
                    if total_supply > 1 {
                        return Err(DefuseError::NftAlreadyDeposited(tid.clone()));
                    }
                }
                TokenId::Nep141(_) | TokenId::Nep245(_) | TokenId::Imt(_) => {}
            }

            owner
                .token_balances
                .add(token_id, amount)
                .ok_or(DefuseError::BalanceOverflow)?;
        }
```

**File:** contracts/poa/token/src/contract.rs (L68-90)
```rust
#[near]
impl PoaFungibleToken for Contract {
    #[only(self, owner)]
    #[payable]
    fn set_metadata(&mut self, metadata: FungibleTokenMetadata) {
        assert_one_yocto();
        metadata.assert_valid();
        self.metadata.set(metadata);
    }

    #[only(self, owner)]
    #[payable]
    fn ft_deposit(&mut self, owner_id: AccountId, amount: U128, memo: Option<String>) {
        self.token.storage_deposit(Some(owner_id.clone()), None);
        self.token.internal_deposit(&owner_id, amount.into());
        FtMint {
            owner_id: &owner_id,
            amount,
            memo: memo.as_deref(),
        }
        .emit();
    }
}
```

**File:** contracts/treasury-logger/src/lib.rs (L45-87)
```rust
#[near]
impl MultiTokenReceiver for Contract {
    fn mt_on_transfer(
        &mut self,
        sender_id: AccountId,
        previous_owner_ids: Vec<AccountId>,
        token_ids: Vec<TokenId>,
        amounts: Vec<U128>,
        msg: String,
    ) -> PromiseOrValue<Vec<U128>> {
        let token = env::predecessor_account_id();

        require!(
            token != env::current_account_id(),
            "self-deposits are forbidden",
        );
        require!(!amounts.is_empty(), "invalid args");
        require!(
            token_ids.len() == amounts.len(),
            "token_ids and amounts length mismatch"
        );
        require!(
            token_ids.len() == previous_owner_ids.len(),
            "token_ids and previous_owner_ids length mismatch"
        );

        Event::MtDeposit {
            token: token.into(),
            sender_id: sender_id.into(),
            previous_owner_ids: previous_owner_ids.iter().map(Into::into).collect(),
            token_ids: token_ids.iter().map(Into::into).collect(),
            amounts: amounts
                .iter()
                .map(|a| a.0)
                .inspect(|a| require!(*a > 0, "zero amount"))
                .collect(),
            msg: msg.into(),
            nonce: self.next_nonce(),
        }
        .emit();

        PromiseOrValue::Value(vec![U128(0); amounts.len()])
    }
```
