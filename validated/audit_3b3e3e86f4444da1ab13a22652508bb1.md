### Title
Exchange creator can freely front-run pending trades by injecting/withdrawing pool balances immediately before a victim's `ExchangeTransactionContract` is mined - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java`, `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
The `ExchangeInjectActuator` and `ExchangeWithdrawActuator` let the exchange's creator freely and instantly rewrite the bonding-curve pool balances (`firstTokenBalance`/`secondTokenBalance`) that determine the swap rate used by `ExchangeTransactionContract`. There is no timelock, no minimum-liquidity floor relative to pending trades, and no restriction preventing the creator from changing balances in the same block window as a pending user trade. This mirrors the Vault plugin-overwrite report: a privileged role (vault owner / exchange creator) can mutate shared state that other unprivileged callers are about to interact with, and can do so atomically and unrestricted, enabling a frontrun that changes the outcome of another user's already-broadcast transaction before it is mined.

### Finding Description
`ExchangeInjectActuator.execute()` recomputes `firstTokenBalance`/`secondTokenBalance` on the `ExchangeCapsule` and immediately commits them via `Commons.putExchangeCapsule(...)`, gated only by the check that the caller is the exchange's `creatorAddress`. [1](#0-0) [2](#0-1) 

`ExchangeWithdrawActuator.execute()` performs the symmetric operation, letting the creator instantly remove liquidity from either side of the pool. [3](#0-2) [4](#0-3) 

The swap rate itself is computed purely from the current `firstTokenBalance`/`secondTokenBalance` at execution time inside `ExchangeProcessor.exchange()`, using a bancor-style formula with no external price oracle or slippage protection tied to the balances observed when the trade was *submitted* versus when it is *mined*. [5](#0-4) 

Because block producers order transactions and a privileged creator (the only account permitted to call inject/withdraw, exactly like the vault owner being the only entity allowed to `install()` plugins) can submit an inject/withdraw transaction that gets included in the same block, immediately before or after a victim's pending `ExchangeTransactionContract`, the creator can:
1. Observe a user's pending swap transaction in the mempool.
2. Submit an `ExchangeWithdrawContract`/`ExchangeInjectContract` that skews the pool ratio unfavorably for the victim just before their trade executes (classic sandwich/front-run), extracting value from the victim's `expected` slippage tolerance.
3. Optionally reverse the balance change immediately afterward, restoring the "legitimate" pool state - directly analogous to the report's "owner can backrun the transaction with another call setting the plugin address back to the legit implementation."

This is structurally the same class of bug: a single privileged party (`creatorAddress` check in `doValidate()`) can overwrite state relied upon by other unprivileged callers' pending transactions, with no restriction, timelock, or invariant check preventing the frontrun. [6](#0-5) 

### Impact Explanation
An exchange creator can extract value (theft of funds) from any counterparty trading against their pool via `ExchangeTransactionContract`, by manipulating the pool ratio immediately before the victim's trade is applied. Because the actuator validate/execute paths are reachable directly from a single signed transaction broadcast by an unprivileged (but pool-creator) account, and the resulting balance changes are irreversible once the victim's swap executes at the skewed rate, this results in concrete unauthorized value extraction from ordinary swap callers.

### Likelihood Explanation
Likelihood is high for any actively-traded exchange pool: the creator only needs to observe pending trades against their own pool (trivial, since they control it and can watch the mempool/next-block inclusion) and submit inject/withdraw transactions with normal fees. No special privilege beyond being the original exchange creator is required, and the actuators impose no cooldown, minimum balance ratio, or trade-in-flight lock.

### Recommendation
Add safeguards to `ExchangeInjectActuator`/`ExchangeWithdrawActuator`, such as: a timelock or cooldown period on creator-initiated pool balance changes; a maximum single-block percentage change in `firstTokenBalance`/`secondTokenBalance`; or requiring inject/withdraw operations to be processed only across maintenance/epoch boundaries rather than atomically within arbitrary blocks alongside pending trades. Additionally, `ExchangeTransactionContract`'s `expected` slippage parameter should be strictly enforced relative to the pool state at submission time, not just at execution time, to blunt sandwich-style front-running.

### Proof of Concept
1. Creator issues `ExchangeCreateContract` for TokenA/TokenB.
2. Victim broadcasts `ExchangeTransactionContract` to swap TokenA for TokenB with an `expected` minimum output based on the current visible pool ratio (validated in `ExchangeTransactionActuator`).
3. Creator observes the pending transaction and broadcasts `ExchangeWithdrawContract` (validated/executed via `ExchangeWithdrawActuator.doValidate()`/`execute()`) removing a large share of TokenB liquidity, worsening the effective rate for the victim while staying within `expected` due to loose bounds, or the creator submits `ExchangeInjectContract` to further skew balances.
4. Both transactions land in the same block (creator controls ordering incentive via fee/timing); victim's swap executes against the skewed `firstTokenBalance`/`secondTokenBalance` computed in `ExchangeProcessor.exchange()`.
5. Creator immediately submits the inverse `ExchangeInjectContract`/`ExchangeWithdrawContract` to restore the original ratio, pocketing the difference extracted from the victim's trade - mirroring the report's Case B (frontrun + backrun with a privileged, unrestricted state-mutation function).

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L57-83)
```java
      ExchangeCapsule exchangeCapsule;
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeInjectContract.getExchangeId()));
      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeInjectContract.getTokenId().toByteArray();
      long tokenQuant = exchangeInjectContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            secondTokenBalance, tokenQuant), firstTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, tokenQuant),
            addExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = floorDiv(multiplyExact(
            firstTokenBalance, tokenQuant), secondTokenBalance);
        exchangeCapsule.setBalance(addExact(firstTokenBalance, anotherTokenQuant),
            addExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L175-177)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L59-89)
```java
      ExchangeCapsule exchangeCapsule = Commons
          .getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(exchangeWithdrawContract.getExchangeId()));

      byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
      byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
      long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
      long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

      byte[] tokenID = exchangeWithdrawContract.getTokenId().toByteArray();
      long tokenQuant = exchangeWithdrawContract.getQuant();

      byte[] anotherTokenID;
      long anotherTokenQuant;

      BigInteger bigFirstTokenBalance = new BigInteger(String.valueOf(firstTokenBalance));
      BigInteger bigSecondTokenBalance = new BigInteger(String.valueOf(secondTokenBalance));
      BigInteger bigTokenQuant = new BigInteger(String.valueOf(tokenQuant));
      if (Arrays.equals(tokenID, firstTokenID)) {
        anotherTokenID = secondTokenID;
        anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, tokenQuant),
            subtractExact(secondTokenBalance, anotherTokenQuant));
      } else {
        anotherTokenID = firstTokenID;
        anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance).longValueExact();
        exchangeCapsule.setBalance(subtractExact(firstTokenBalance, anotherTokenQuant),
            subtractExact(secondTokenBalance, tokenQuant));
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L139-183)
```java
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    if (!this.any.is(ExchangeWithdrawContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeWithdrawContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeWithdrawContract contract;
    try {
      contract = this.any.unpack(ExchangeWithdrawContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

    if (!DecodeUtil.addressValid(ownerAddress)) {
      throw new ContractValidateException("Invalid address");
    }

    if (!accountStore.has(ownerAddress)) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] not exists");
    }

    AccountCapsule accountCapsule = accountStore.get(ownerAddress);

    if (accountCapsule.getBalance() < calcFee()) {
      throw new ContractValidateException("No enough balance for exchange withdraw fee!");
    }

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId() + ActuatorConstant
          .NOT_EXIST_STR);
    }

    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-45)
```java
  private long exchangeToSupply(long balance, long quant) {
    logger.debug("balance: " + balance);
    long newBalance = balance + quant;
    logger.debug("balance + quant: " + newBalance);

    double issuedSupply = -supply * (1.0
        - Maths.pow(1.0 + (double) quant / newBalance, 0.0005, this.useStrictMath));
    logger.debug("issuedSupply: " + issuedSupply);
    long out = (long) issuedSupply;
    supply += out;

    return out;
  }

  private long exchangeFromSupply(long balance, long supplyQuant) {
    supply -= supplyQuant;

    double exchangeBalance = balance
        * (Maths.pow(1.0 + (double) supplyQuant / supply, 2000.0, this.useStrictMath) - 1.0);
    logger.debug("exchangeBalance: " + exchangeBalance);

    return (long) exchangeBalance;
  }

  @Override
  public long exchange(long sellTokenBalance, long buyTokenBalance, long sellTokenQuant) {
    long relay = exchangeToSupply(sellTokenBalance, sellTokenQuant);
    return exchangeFromSupply(buyTokenBalance, relay);
  }
```
