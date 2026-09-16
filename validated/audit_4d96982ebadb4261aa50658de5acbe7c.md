### Title
`Exchange` (TRC10 bancor-style AMM) never normalizes for each asset's `precision`, letting a user create/inject/trade pools that misvalue tokens with different decimal precisions - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java])

### Summary
The `ConvexRewardPoolOracle` bug is caused by treating raw integer token amounts as if 1 whole unit of one token equals 1 whole unit of another token, without correcting for differing `decimals()`. The same class of bug exists in java-tron's on-chain TRC10 "Exchange" (Bancor-style constant-formula AMM): `ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`, and the underlying `ExchangeCapsule.transaction()` / `ExchangeProcessor` / `SafeExchangeProcessor` all operate purely on raw `long` balances (smallest integer unit) of `firstTokenId`/`secondTokenId`, with zero reference to each TRC10 asset's `precision` field.

### Finding Description
TRC10 assets carry a configurable `precision` (0–6, validated in `AssetIssueActuator`) that determines how many decimal places one "whole" unit of the asset represents; `AssetIssueCapsule` stores/exposes this via `getPrecision()`. [1](#0-0) 

Any account can call `ExchangeCreateContract` to create a liquidity pool pairing two arbitrary TRX/TRC10 tokens with arbitrary integer balances, with validation only checking non-zero balances and a global balance limit — never comparing or normalizing the two tokens' `precision`: [2](#0-1) 

The pool's pricing/exchange math (`ExchangeCapsule.transaction`, backed by `ExchangeProcessor`/`SafeExchangeProcessor`) then computes swap/inject/withdraw amounts directly from these raw integer balances, implicitly assuming that 1 raw unit of `firstToken` and 1 raw unit of `secondToken` are economically comparable: [3](#0-2) [4](#0-3) 

`ExchangeInjectActuator` and `ExchangeWithdrawActuator` similarly compute proportional amounts of the "other" token purely from the existing raw-integer balance ratio, again with no reference to token `precision`: [5](#0-4) [6](#0-5) 

None of the Exchange actuators (`ExchangeCreateActuator`, `ExchangeInjectActuator`, `ExchangeWithdrawActuator`, `ExchangeTransactionActuator`) reference the `precision` field at all (confirmed by searching the actuator sources for `precision` — zero matches), whereas `AssetIssueActuator` explicitly enforces and stores per-asset `precision` (0–6) at issuance time. This mirrors WATCHPUG's finding exactly: a pricing/exchange mechanism silently assumes that raw integer units of two different fungible tokens are directly comparable, when in fact the tokens can have different decimal precisions.

### Impact Explanation
Since a TRC10 issuer freely chooses `precision` in `[0,6]` at asset-issue time, an attacker can issue two assets with different `precision` values (e.g., 0 and 6) and then create/inject into an `Exchange` pool pairing them, or pair such an asset against TRX (which is fixed at 6 decimals, i.e., "SUN" units). Because the AMM's constant-formula pricing treats raw balances as fungible "whole units," the effective real-world exchange rate embedded in the pool can be off by orders of magnitude (up to 10^6) relative to the intended 1:1-esque calibration the pool creator intended. Other users trading against such a mispriced pool (via `ExchangeTransactionActuator`) can extract TRX/TRC10 value from counterparties, or the pool creator can drain funds by exploiting third parties who trade against the pool believing quantities behave uniformly. This can lead to theft of funds through the exchange mechanism.

### Likelihood Explanation
Likelihood is High: creating a TRC10 asset with a custom `precision` and creating/funding an `Exchange` pool are both ordinary, unprivileged transactions available to any account (`AssetIssueContract`, `ExchangeCreateContract`, `ExchangeInjectContract`, `ExchangeTransactionContract`) — no special permission, SR/witness status, or off-chain trust is required. The bug is a systemic design gap (missing precision-normalization) rather than a rare edge case, so it is trivially and repeatably reachable by any anonymous API/transaction broadcaster.

### Recommendation
When creating an `Exchange` pool (`ExchangeCreateActuator.doValidate`/`execute`) and whenever injecting/withdrawing (`ExchangeInjectActuator`, `ExchangeWithdrawActuator`), fetch each token's `precision` via `AssetIssueStore`/`AssetIssueCapsule.getPrecision()` (treating TRX as precision 6) and normalize balances/quantities to a common base before applying the Bancor/constant-formula math in `ExchangeProcessor`/`SafeExchangeProcessor`, or explicitly document/enforce that pool creators must supply balances already scaled to a canonical precision, with on-chain validation rejecting mismatched, unnormalized precision pairs (similar in spirit to WATCHPUG's suggested `sanityCheck()`).

### Proof of Concept
1. Attacker issues TRC10 asset `A` with `precision = 0` via `AssetIssueContract` (validated ≤6 in `AssetIssueActuator`).
2. Attacker issues TRC10 asset `B` with `precision = 6` via `AssetIssueContract`.
3. Attacker calls `ExchangeCreateContract` to create a pool with `firstTokenBalance` of `A` = 1,000,000 (raw units, i.e., 1,000,000 "whole" A given precision 0) and `secondTokenBalance` of `B` = 1,000,000 (raw units, i.e., 1 "whole" B given precision 6) — `ExchangeCreateActuator.doValidate` accepts this since it never checks precision consistency. [7](#0-6) 
4. A victim, assuming the pool prices `A` and `B` in comparable "whole token" terms (1,000,000 A : 1 B), calls `ExchangeTransactionActuator` to swap into/out of the pool at the raw-unit ratio actually encoded (1,000,000 A : 1,000,000 B raw units = 1,000,000 A : 1 B whole), receiving/paying amounts wildly different from the intended real-world value, allowing the attacker to profit from the mispriced pool. [8](#0-7)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/AssetIssueCapsule.java (L1-1)
```java
/*
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L145-231)
```java
  private boolean doValidate() throws ContractValidateException {
    if (this.any == null) {
      throw new ContractValidateException(ActuatorConstant.CONTRACT_NOT_EXIST);
    }
    if (chainBaseManager == null) {
      throw new ContractValidateException(ActuatorConstant.STORE_NOT_EXIST);
    }
    AccountStore accountStore = chainBaseManager.getAccountStore();
    DynamicPropertiesStore dynamicStore = chainBaseManager.getDynamicPropertiesStore();
    if (!this.any.is(ExchangeCreateContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeCreateContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeCreateContract contract;
    try {
      contract = this.any.unpack(ExchangeCreateContract.class);
    } catch (InvalidProtocolBufferException e) {
      throw new ContractValidateException(e.getMessage());
    }

    byte[] ownerAddress = contract.getOwnerAddress().toByteArray();
    String readableOwnerAddress = StringUtil.createReadableString(ownerAddress);

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

    byte[] firstTokenID = contract.getFirstTokenId().toByteArray();
    byte[] secondTokenID = contract.getSecondTokenId().toByteArray();
    long firstTokenBalance = contract.getFirstTokenBalance();
    long secondTokenBalance = contract.getSecondTokenBalance();

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

    if (Arrays.equals(firstTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(firstTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(firstTokenID, firstTokenBalance, dynamicStore)) {
        throw new ContractValidateException("first token balance is not enough");
      }
    }

    if (Arrays.equals(secondTokenID, TRX_SYMBOL_BYTES)) {
      if (accountCapsule.getBalance() < addExact(secondTokenBalance, calcFee())) {
        throw new ContractValidateException("balance is not enough");
      }
    } else {
      if (!accountCapsule.assetBalanceEnoughV2(secondTokenID, secondTokenBalance, dynamicStore)) {
        throw new ContractValidateException("second token balance is not enough");
      }
    }

    return true;
  }
```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-169)
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L60-99)
```java
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

      long newBalance = subtractExact(accountCapsule.getBalance(), calcFee());
      accountCapsule.setBalance(newBalance);

      if (Arrays.equals(tokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, tokenQuant));
      } else {
        accountCapsule.reduceAssetAmountV2(tokenID, tokenQuant, dynamicStore, assetIssueStore);
      }

      if (Arrays.equals(anotherTokenID, TRX_SYMBOL_BYTES)) {
        accountCapsule.setBalance(subtractExact(newBalance, anotherTokenQuant));
      } else {
        accountCapsule
            .reduceAssetAmountV2(anotherTokenID, anotherTokenQuant, dynamicStore, assetIssueStore);
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L74-89)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L61-91)
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
```
