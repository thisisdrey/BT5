### Title
Sequential, reorg-mutable `exchangeId` allows an attacker to redirect a victim's pending `ExchangeTransactionContract` to an attacker-controlled token pool - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java])

### Summary
`ExchangeCreateActuator` assigns a brand-new Bancor-style liquidity pool ("Exchange") a purely sequential numeric `exchangeId` computed from global chain state at execution time (`getLatestExchangeNum() + 1`), exactly the same pattern the external report flags as unsafe: the identifier is not derived from the pool's own content (creator, token pair) but from a mutable counter. `ExchangeTransactionActuator`, `ExchangeInjectActuator` and `ExchangeWithdrawActuator` all resolve the target pool purely by this numeric id supplied in the signed contract, with no binding to the specific pool/creator/token-pair the signer actually intended when they built and signed the transaction.

### Finding Description
`ExchangeCreateActuator.execute()` computes the new pool id like this: [1](#0-0) 

and persists it via `dynamicStore.saveLatestExchangeNum(id)` [2](#0-1) . This id depends solely on how many `ExchangeCreateContract` transactions have been *applied so far* — it is not a hash of the pool's owner/token pair, so any reordering/omission of prior `ExchangeCreateContract` transactions (e.g., a chain re-organization) changes which token pair ends up under a given `exchangeId`.

Any subsequent operation — a trade, injection, or withdrawal — resolves the target purely by that numeric id: [3](#0-2) 

and in validation: [4](#0-3) 

The only safety checks are that `tokenID` matches one of the (possibly substituted) exchange's two tokens, and a slippage floor: [5](#0-4) 

Neither check verifies that the exchange still belongs to the creator/token-pair the signer originally intended. `ExchangeCreateContract` itself requires no special privilege — only that the owner account exists and has sufficient balance [6](#0-5)  — so any ordinary account (an unprivileged "order placer"/asset issuer, in the terminology of the reachable-actor list) can create pools and race to claim a target `exchangeId`.

This mirrors the external report's root cause precisely: an id computed from mutable, execution-time chain state (`proposalCount`/`getLatestProposalNum()` there, `getLatestExchangeNum()` here) is later referenced by a separate, already-signed transaction (`confirmTransaction`/`ExchangeTransactionContract`) purely by that number, with no cryptographic binding to the original object's identity.

### Impact Explanation
If a re-organization (or any transaction-ordering perturbation prior to finality) causes a different `ExchangeCreateContract` to occupy the `exchangeId` that a victim's already-signed, pending `ExchangeTransactionContract` (or `ExchangeInjectContract`/`ExchangeWithdrawContract`) references, the victim's transaction will execute against a completely different — attacker-controlled — liquidity pool instead of the one they intended. An attacker who front-runs the reorg with an `ExchangeCreateContract` whose `firstTokenId`/`secondTokenId` includes the token the victim is selling, paired with a worthless token the attacker controls, and funds the pool with a large enough quantity of that worthless token to trivially satisfy the victim's `expected` slippage floor, can cause the victim's real, valuable asset to be swapped away for the attacker's worthless token. This is a direct, unauthorized transfer/theft of victim funds (an unbacked-value transfer to the attacker), not merely a resource or availability issue.

### Likelihood Explanation
Exploitation requires a chain re-organization (or comparable non-finalized reordering window) affecting the ordering of `ExchangeCreateContract` transactions relative to a victim's pending exchange-referencing transaction, plus the attacker being able to observe the victim's pending transaction (exchangeId, tokenId, quant, expected are all visible fields of a broadcast/pending transaction) and race an `ExchangeCreateContract` into the freed id slot. Deep reorgs are rarer on java-tron's DPoS consensus than on some other chains, but the report explicitly notes such reorgs have been observed on other production chains, and no privileged role is required to create Exchanges or reference them by id — only ordinary account balance is needed.

### Recommendation
Do not resolve exchanges (or any similarly-created object: markets, future features) purely by a monotonically-incrementing counter captured in a separate later transaction. Either (a) require the transaction that references an existing pool to also assert the expected creator address and/or token pair, validated against the resolved `ExchangeCapsule`, and reject on mismatch, or (b) derive `exchangeId` deterministically from the pool's content (creator address + token pair + a nonce/hash), analogous to the mitigation applied for the reported governance-proposal bug, so that a resolved id can never silently refer to a different pool after a reordering event.

### Proof of Concept
1. Victim broadcasts `ExchangeTransactionContract{exchangeId=N, tokenId=TRX, quant=Q, expected=E}` intending to trade against an already-existing Exchange `N` (TRX/USDJ pool) created earlier by a legitimate `ExchangeCreateContract`.
2. Before the victim's transaction is included/finalized, a re-organization drops the block(s) containing the original `ExchangeCreateContract` for pool `N`.
3. Attacker races an `ExchangeCreateContract{firstTokenId=TRX, secondTokenId=JUNK}` into the freed slot so that `getLatestExchangeNum()+1 == N` again, funding the pool heavily with `JUNK` (attacker's own worthless asset) so that `exchangeCapsule.transaction(...)` returns an `anotherTokenQuant >= E` (see the slippage check at [5](#0-4) ).
4. The victim's original `ExchangeTransactionContract` (still referencing `exchangeId=N`) is now applied against the attacker's TRX/JUNK pool via `ExchangeTransactionActuator.execute()` [7](#0-6) , transferring the victim's real TRX to the attacker's pool and crediting the victim with worthless `JUNK` instead of the intended USDJ.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L78-79)
```java
      long id = addExact(dynamicStore.getLatestExchangeNum(), 1);
      long now = dynamicStore.getLatestBlockHeaderTimestamp();
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L118-119)
```java
      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
      dynamicStore.saveLatestExchangeNum(id);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L169-181)
```java
    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + NOT_EXIST_STR);
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange create fee!");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L57-99)
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

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
      } else {
        anotherTokenID = firstTokenID;
      }

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);

      Commons.putExchangeCapsule(exchangeCapsule, dynamicStore, exchangeStore, exchangeV2Store,
          assetIssueStore);

      ret.setExchangeReceivedAmount(anotherTokenQuant);
      ret.setStatus(fee, code.SUCESS);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L159-182)
```java
    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId()
          + ActuatorConstant.NOT_EXIST_STR);
    }

    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();
    long tokenExpected = contract.getExpected();

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }
    if (!Arrays.equals(tokenID, firstTokenID) && !Arrays.equals(tokenID, secondTokenID)) {
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```
