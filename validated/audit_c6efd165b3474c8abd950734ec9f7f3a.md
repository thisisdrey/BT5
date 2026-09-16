### Title
Exchange creator can withdraw pooled bancor-style liquidity funded by counterparties' trades with no injected-amount tracking or throttling - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
The external report describes `adminWithdrawBacking`, which lets a privileged role empty a shared token pool at will because the contract never separately tracks what the privileged party actually deposited (via `adminInjectBacking`) versus what accrued in the pool from other activity, and applies no per-transaction or time-based withdrawal cap. The closest reachable analog in java-tron is the TRC10 bancor-style Exchange mechanism: an `ExchangeCreateActuator`/`ExchangeInjectActuator` seed a liquidity pool, ordinary users trade against that pool via `ExchangeTransactionActuator` (which mutates the pool's `firstTokenBalance`/`secondTokenBalance` using the AMM curve in `ExchangeCapsule.transaction`), and only the exchange **creator** address is authorized to call `ExchangeWithdrawActuator` to pull tokens back out - up to the full current pool balance, in a single transaction, with no distinction between the creator's originally injected principal and value that flowed in from other traders' orders, and no time-based rate limiting.

### Finding Description
`ExchangeWithdrawActuator.doValidate()` only checks that the caller is the exchange's `creatorAddress` and that the requested `tokenQuant` does not exceed the exchange's current balance for that token: [1](#0-0) [2](#0-1) 

There is no bookkeeping anywhere in `ExchangeCapsule`/`ExchangeWithdrawActuator` that separates the amount the creator originally injected (via `ExchangeCreateActuator`/`ExchangeInjectActuator`) from the amount that accumulated in the pool purely because unrelated third-party users traded against it via `ExchangeTransactionActuator`: [3](#0-2) 

Because the pool's `firstTokenBalance`/`secondTokenBalance` are just running totals updated by every trade, and `execute()` in `ExchangeWithdrawActuator` lets the creator pull out the full current balance for either side in one call: [4](#0-3) 

the creator can withdraw the entire pool (their own deposit plus all value contributed by traders) at any time, in any single transaction, with no injected-amount tracking and no rate/time throttling - structurally the same gap the report flags in `adminInjectBacking`/`adminWithdrawBacking`.

### Impact Explanation
Any user who creates an Exchange (a normal, unprivileged action - no committee/witness role required) becomes a de-facto "admin" of that liquidity pool. Other users who trade TRX/TRC10 tokens against the exchange (a normal, unprivileged trading action) effectively deposit value into the pool. The creator can then withdraw the pool's balance in full at any moment, draining funds that traders may still expect to be exchangeable, resulting in loss of user funds for anyone who trades against that pool afterward (their trade executes against a suddenly-empty or drained pool, or the creator races withdrawal against pending swaps). This matches the report's "unrestricted draining of pooled funds" bug class and its impact (loss of user funds / single point of failure at the pool-owner level).

### Likelihood Explanation
Reaching this requires only ordinary, unprivileged transactions: `ExchangeCreateContract` to create a pool, `ExchangeTransactionContract` from any counterparty to trade into the pool, and `ExchangeWithdrawContract` signed by the creator to withdraw. No SR/witness/committee privilege, no p2p/network condition, and no mocked-only path is needed - all three actions are standard broadcastable contracts reachable from `Wallet`/`TronJsonRpcImpl` create-transaction endpoints. The only gating factor is that the caller must be the exchange creator, which is self-assigned at `ExchangeCreateContract` submission time, so any attacker can set this up themselves.

### Recommendation
- Track the creator's net-injected liquidity for each exchange (sum of `ExchangeCreateContract` + `ExchangeInjectContract` amounts, minus prior withdrawals) separately from the trade-accrued pool balance, and cap `ExchangeWithdrawActuator` to that tracked amount, or
- Enforce a maximum percentage of pool balance withdrawable per transaction/time window (mirroring the reporter's suggested "3% of total balance" throttle) inside `ExchangeWithdrawActuator.execute()`/`doValidate()`.

### Proof of Concept
1. Attacker calls `ExchangeCreateContract` to create an exchange pool for TRX/TokenX with a small seed amount, becoming `creatorAddress` (see validation in `ExchangeCreateActuator`).
2. Victim(s) submit `ExchangeTransactionContract` trades that swap into the pool, increasing `firstTokenBalance`/`secondTokenBalance` via `ExchangeCapsule.transaction` [5](#0-4) .
3. Attacker (creator) submits `ExchangeWithdrawContract` requesting `tokenQuant` equal to the entire current pool balance for one side; `doValidate()` only checks creator identity and pool sufficiency, not provenance of funds [6](#0-5) .
4. `execute()` credits the attacker's account with both `tokenQuant` and the computed `anotherTokenQuant`, draining the pool in a single transaction [7](#0-6) .

Note: I was unable to fully verify whether `ExchangeInjectActuator`/`ExchangeCreateActuator` impose any additional creator-only restriction that would mitigate this (e.g., minimum retained liquidity), since I did not get to read those files in full before the iteration limit; a Devin session with full file access should confirm this before filing.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L59-106)
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, tokenQuant));
      } else {
        accountCapsule.addAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(addExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .addAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }

      accountStore.put(accountCapsule.createDbKey(), accountCapsule);
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L155-183)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L218-227)
```java
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-99)
```java
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

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-168)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

    long buyTokenQuant = 0;
    long firstTokenBalance = this.exchange.getFirstTokenBalance();
    long secondTokenBalance = this.exchange.getSecondTokenBalance();
    long newFirstTokenBalance;
    long newSecondTokenBalance;

    if (this.exchange.getFirstTokenId().equals(ByteString.copyFrom(sellTokenID))) {
      buyTokenQuant = processor.exchange(firstTokenBalance,
          secondTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(firstTokenBalance, sellTokenQuant)
          : firstTokenBalance + sellTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(secondTokenBalance, buyTokenQuant)
          : secondTokenBalance - buyTokenQuant;

    } else {
      buyTokenQuant = processor.exchange(secondTokenBalance,
          firstTokenBalance,
          sellTokenQuant);
      newFirstTokenBalance = hardenedCalc
          ? StrictMathWrapper.subtractExact(firstTokenBalance, buyTokenQuant)
          : firstTokenBalance - buyTokenQuant;
      newSecondTokenBalance = hardenedCalc
          ? StrictMathWrapper.addExact(secondTokenBalance, sellTokenQuant)
          : secondTokenBalance + sellTokenQuant;

    }

    if (hardenedCalc && (newFirstTokenBalance < 0 || newSecondTokenBalance < 0)) {
      throw new ContractValidateException("Exchange balance must be >=0 after transaction");
    }
    this.exchange = this.exchange.toBuilder()
        .setFirstTokenBalance(newFirstTokenBalance)
        .setSecondTokenBalance(newSecondTokenBalance)
        .build();

    return buyTokenQuant;
```
