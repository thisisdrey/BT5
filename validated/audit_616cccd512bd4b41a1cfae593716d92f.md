### Title
Missing slippage protection in `ExchangeInjectContract`/`ExchangeWithdrawContract` enables sandwich attacks against TRC10 Bancor-style exchanges - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
Java-tron's native TRC10 "Exchange" (Bancor-style AMM) implements slippage protection only for `ExchangeTransactionContract` via the user-supplied `expected` field, which is checked against the computed output before executing the trade. `ExchangeInjectContract` and `ExchangeWithdrawContract`, however, compute the counterpart token amount purely from the exchange's *live* pool ratio at execution time, with no caller-supplied bound and no check against a minimum/maximum expected amount. Because `ExchangeTransactionContract` can be submitted by any account holding the relevant token/TRX, an attacker can manipulate the pool ratio immediately before and after a victim's inject/withdraw transaction, extracting value from the victim in a classic sandwich attack — the exact bug class described in the reference report ("hardcoded/absent slippage bound allows sandwiching").

### Finding Description
`ExchangeTransactionActuator` requires and validates a caller-chosen `expected` minimum-output field: [1](#0-0) 

In contrast, `ExchangeWithdrawActuator.execute()` derives `anotherTokenQuant` solely from the current `firstTokenBalance`/`secondTokenBalance` ratio, with no user-supplied minimum bound parameter in the protobuf contract at all: [2](#0-1) 

The `doValidate()` method for withdraw only checks that the *computed* ratio is internally "precise enough" (a rounding-consistency check, not a slippage bound against attacker manipulation): [3](#0-2) 

`ExchangeInjectActuator` has the identical structural issue — it computes `anotherTokenQuant` from the live pool balances with no expected/min-out bound at all: [4](#0-3) 

Critically, `ExchangeTransactionContract` is callable by **any account** that owns the sold token/TRX — there is no restriction to the exchange creator: [5](#0-4) 

This means an unprivileged attacker can:
1. Observe a pending `ExchangeInjectContract`/`ExchangeWithdrawContract` from the exchange creator in the mempool.
2. Front-run it with an `ExchangeTransactionContract` trade that skews `firstTokenBalance`/`secondTokenBalance` in the attacker's favor.
3. Let the victim's inject/withdraw execute against the manipulated ratio (injecting too much value, or withdrawing too little).
4. Back-run with an opposite `ExchangeTransactionContract` trade to restore the ratio, pocketing the difference extracted from the victim, while paying only bandwidth/energy cost.

This mirrors the root cause of the referenced report: a swap/liquidity operation whose output is computed from a manipulable live price with no caller-enforced minimum/maximum bound, unlike the sibling `ExchangeTransactionContract` path, which already implements the correct pattern.

### Impact Explanation
The exchange creator (the only party allowed to call inject/withdraw, per the `creator` check in `doValidate()`) can lose funds during withdraw (receiving less of the counterpart token than the pool's undisturbed ratio would provide) or overpay during inject (contributing more value than the fair ratio requires), with the difference captured by the attacker. This is a concrete unauthorized value-extraction/fund-loss vector reachable purely by broadcasting ordinary signed transactions (`ExchangeTransactionContract` sandwich around `ExchangeInjectContract`/`ExchangeWithdrawContract`), consistent with the "theft of funds" acceptance criteria.

### Likelihood Explanation
Any TRC10 exchange pool with meaningful liquidity/value is a potential target. The attack requires only the ability to submit transactions (bandwidth/energy) and knowledge of the mempool — no special privilege, SR/witness status, or off-chain trust is needed. The larger the withdraw/inject relative to pool depth, the more profitable the sandwich, similar to any AMM sandwich scenario.

### Recommendation
Add a caller-supplied `expected`/bound field (min output for withdraw, max another-token cost for inject) to `ExchangeInjectContract` and `ExchangeWithdrawContract`, mirroring the pattern already used in `ExchangeTransactionContract`, and validate the computed `anotherTokenQuant` against it in both `ExchangeInjectActuator.doValidate()`/`execute()` and `ExchangeWithdrawActuator.doValidate()`/`execute()` before mutating balances.

### Proof of Concept
1. Attacker monitors mempool for a pending `ExchangeWithdrawContract` from exchange creator `C` withdrawing `tokenQuant` of `firstTokenID`.
2. Attacker submits `ExchangeTransactionContract` selling a large amount of `secondTokenID` into the same exchange, shifting `firstTokenBalance`/`secondTokenBalance` so that the ratio used in `ExchangeWithdrawActuator.execute()` (lines 79-80/85-86) yields a lower `anotherTokenQuant` for the victim's withdraw.
3. Victim's `ExchangeWithdrawContract` executes at the skewed ratio, receiving less counterpart token than expected; there is no `expected`/min-out field to reject this.
4. Attacker submits an opposite `ExchangeTransactionContract` to restore the ratio and realize the captured spread, net of trading and energy costs.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L119-157)
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
    ExchangeStore exchangeStore = chainBaseManager.getExchangeStore();
    ExchangeV2Store exchangeV2Store = chainBaseManager.getExchangeV2Store();
    if (!this.any.is(ExchangeTransactionContract.class)) {
      throw new ContractValidateException(
          "contract type error,expected type [ExchangeTransactionContract],real type[" + any
              .getClass() + "]");
    }
    final ExchangeTransactionContract contract;
    try {
      contract = this.any.unpack(ExchangeTransactionContract.class);
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
      throw new ContractValidateException("No enough balance for exchange transaction fee!");
    }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L214-243)
```java
    BigDecimal bigFirstTokenBalance = new BigDecimal(String.valueOf(firstTokenBalance));
    BigDecimal bigSecondTokenBalance = new BigDecimal(String.valueOf(secondTokenBalance));
    BigDecimal bigTokenQuant = new BigDecimal(String.valueOf(tokenQuant));
    final boolean allowHarden = allowHarden();
    if (Arrays.equals(tokenID, firstTokenID)) {
      anotherTokenQuant = bigSecondTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigFirstTokenBalance).longValueExact();
      if (firstTokenBalance < tokenQuant || secondTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }
      if (allowHarden) {
        BigDecimal remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigSecondTokenBalance.multiply(bigTokenQuant)
            .divide(bigFirstTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L65-83)
```java
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
