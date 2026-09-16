I've confirmed the analog in java-tron's `ExchangeCreateActuator`. The finding is solid: java-tron's TRX/TRC10 AMM-style Exchange feature allows unlimited duplicate pools for the same token pair, and downstream consumers (traders via `ExchangeTransactionActuator`, and any off-chain integrator) select pools purely by numeric `exchange_id` with no canonical/liquidity-based selection — closely mirroring the Uniswap V3 "multiple pools per pair" issue in the report.

### Title
Unlimited duplicate Bancor-style Exchange pools can be created for the same token pair, enabling cheap price manipulation of low-liquidity shadow pools - (File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java)

### Summary
`ExchangeCreateActuator` lets any account with the `EXCHANGE_CREATE_FEE` create a new Bancor-formula AMM pool (`ExchangeCapsule`) for an arbitrary pair of TRX/TRC10 tokens. There is no check that a pool for that exact `(firstTokenId, secondTokenId)` pair already exists, so an unlimited number of independent pools can coexist for the same pair, each with its own liquidity and price, exactly analogous to the reported Uniswap V3 issue where multiple fee-tier pools can exist for one token pair.

### Finding Description
In `doValidate()` of `ExchangeCreateActuator`, the only pair-related checks are that `firstTokenID != secondTokenID` and that both token IDs are well-formed/numeric when `AllowSameTokenName` is active: [1](#0-0) 
There is no lookup against `ExchangeStore`/`ExchangeV2Store` to see if a pool for that pair already exists. In `execute()`, a brand-new `id` (`getLatestExchangeNum()+1`) is always assigned and a new `ExchangeCapsule` is persisted: [2](#0-1) 
Consequently, any address can seed a pool for a popular pair (e.g. TRX/USDD-like TRC10) with minimal liquidity (bounded only by `getExchangeBalanceLimit()` on the low end — any positive balance is accepted per `firstTokenBalance <= 0 || secondTokenBalance <= 0` check): [3](#0-2) 
Trading against a specific pool is done purely by numeric `exchange_id`, chosen by the caller, in `ExchangeTransactionActuator`: [4](#0-3) 
No mechanism ranks or canonicalizes pools by liquidity, so wallets, DApps, or any integrator that queries `ListExchanges`/exchange-by-id and blindly picks "an" exchange for a pair (rather than the deepest one) can be routed to an attacker-seeded, thinly-liquidated pool.

### Impact Explanation
Because the Bancor formula's price impact is inversely related to pool liquidity, an attacker-created low-liquidity duplicate pool for a legitimate pair can be manipulated to an arbitrary price with a small amount of TRX/TRC10, using `ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract` calls the attacker fully controls. Any counterparty (user, exchange integrator, or automated tool) that trades against that specific `exchange_id` — believing it represents "the" market pool for the pair — can suffer direct economic loss from the manipulated price, i.e. unauthorized value transfer/theft of funds mediated entirely through a standard signed transaction flow.

### Likelihood Explanation
Creating an exchange only costs the fixed `EXCHANGE_CREATE_FEE` (default 1024 TRX) and requires no special privilege — any funded account can call `ExchangeCreateContract`. Because token pairs are commonly referenced only by IDs (e.g. TRX ⇄ some TRC10), an attacker can proactively create duplicate low-liquidity pools for high-value pairs and wait for unsuspecting counterparties to route into them.

### Recommendation
Enforce pair-uniqueness in `ExchangeCreateActuator.doValidate()`/`execute()`: before creating a new pool, check `ExchangeStore`/`ExchangeV2Store` (or a dedicated pair index) for an existing pool with the same `(firstTokenId, secondTokenId)` (in either order) and reject the creation, or expose a canonical "get exchange by pair" lookup (analogous to `getMarketPriceByPair` for the order-book market) that always resolves to the highest-liquidity pool, so wallets/integrators are not left to guess which `exchange_id` is authoritative for a pair.

### Proof of Concept
1. Attacker account A calls `ExchangeCreateContract` with `first_token_id = TRX`, `second_token_id = <popular TRC10>`, `first_token_balance = 1`, `second_token_balance = 1` (minimal but > 0), paying the `EXCHANGE_CREATE_FEE`. This succeeds because `doValidate()` never checks for an existing pool with the same pair — see [5](#0-4) . A new `exchange_id` (e.g. `N`) is minted even though a deep, legitimate pool `M` for the identical pair already exists.
2. Attacker uses `ExchangeTransactionContract` against `exchange_id = N` to move its price arbitrarily (cheap due to tiny liquidity), per the Bancor math in `ExchangeCapsule.transaction()`/`ExchangeProcessor.exchange()` invoked from `ExchangeTransactionActuator.execute()` — see [6](#0-5) .
3. Victim (wallet/integrator) queries exchanges for the pair (e.g. via `ListExchanges`) and, absent any canonical pair→pool mapping, selects pool `N` to submit an `ExchangeTransactionContract` trade, receiving the manipulated (unfavorable) rate and suffering an economic loss to the attacker who then rebalances/drains pool `N`.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L78-116)
```java
      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
      if (dynamicStore.getAllowSameTokenName() == 0) {
        //save to old asset store
        ExchangeCapsule exchangeCapsule =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsule.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeStore.put(exchangeCapsule.createDbKey(), exchangeCapsule);

        //save to new asset store
        if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
          String firstTokenRealID = assetIssueStore.get(firstTokenID).getId();
          firstTokenID = firstTokenRealID.getBytes();
        }
        if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
          String secondTokenRealID = assetIssueStore.get(secondTokenID).getId();
          secondTokenID = secondTokenRealID.getBytes();
        }
      }

      {
        // only save to new asset store
        ExchangeCapsule exchangeCapsuleV2 =
            new ExchangeCapsule(
                exchangeCreateContract.getOwnerAddress(),
                id,
                now,
                firstTokenID,
                secondTokenID
            );
        exchangeCapsuleV2.setBalance(firstTokenBalance, secondTokenBalance);
        exchangeV2Store.put(exchangeCapsuleV2.createDbKey(), exchangeCapsuleV2);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L188-208)
```java
    if (dynamicStore.getAllowSameTokenName() == 1) {
      if (!Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES) && !isNumber(firstTokenID)) {
        throw new ContractValidateException("first token id is not a valid number");
      }
      if (!Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES) && !isNumber(secondTokenID)) {
        throw new ContractValidateException("second token id is not a valid number");
      }
    }

    if (Arrays.equals(firstTokenID, secondTokenID)) {
      throw new ContractValidateException("cannot exchange same tokens");
    }

    if (firstTokenBalance <= 0 || secondTokenBalance <= 0) {
      throw new ContractValidateException("token balance must greater than zero");
    }

    long balanceLimit = dynamicStore.getExchangeBalanceLimit();
    if (firstTokenBalance > balanceLimit || secondTokenBalance > balanceLimit) {
      throw new ContractValidateException("token balance must less than " + balanceLimit);
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-69)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeTransactionContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();

      byte[] tokenID = exchangeTransactionContract.getTokenId().toByteArray();
      long tokenQuant = exchangeTransactionContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
          dynamicStore.allowStrictMath(), allowHarden());
```
