### Title
Missing slippage/minimum-output protection in `ExchangeWithdrawContract` allows unbounded value extraction on liquidity withdrawal - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
`ExchangeWithdrawContract`, unlike its sibling `ExchangeTransactionContract`, has no field letting the caller bound the minimum amount of the paired token they are willing to accept when withdrawing liquidity from a Bancor-style TRC10 exchange pair. The amount actually received is computed purely from the pool's current ratio at execution time, so a transaction can be sandwiched to reduce the withdrawer's payout with no on-chain limit the user can set — the same "unbounded, unlimited-by-user" fee/loss pattern described in the external Pod report.

### Finding Description
`ExchangeWithdrawContract` only carries `owner_address`, `exchange_id`, `token_id`, and `quant` [1](#0-0) . Compare this to `ExchangeTransactionContract`, which additionally carries an `expected` field representing "expected minimum number of tokens" [2](#0-1) . That `expected` field is enforced in `ExchangeTransactionActuator.doValidate()`, which rejects the transaction if the computed output is less than what the user requested: `if (anotherTokenQuant < tokenExpected) { throw new ContractValidateException("token required must greater than expected"); }` [3](#0-2) .

`ExchangeWithdrawActuator.execute()` has no equivalent guard. It computes `anotherTokenQuant` directly from the current pool balances at execution time using the Bancor ratio math, and immediately credits the account with whatever value results — with zero minimum-output check: [4](#0-3) . `ExchangeWithdrawActuator.doValidate()` similarly performs no check comparing the resulting `anotherTokenQuant` to any user-supplied minimum, because no such field exists on the contract at all [5](#0-4) .

Because Super Representatives order transactions within a block, an attacker can submit `ExchangeTransactionContract` (buy/sell) calls against the same `exchange_id` immediately before the victim's `ExchangeWithdrawContract` is applied, and reverse the trade immediately after, sandwiching the pool ratio at the moment of withdrawal. Since the withdrawer cannot specify any acceptable minimum for the paired token, the entire economic outcome of the withdrawal is dictated by whoever controls the pool ratio at execution time — exactly the "fee/loss not limited by the user" bug class flagged in the Pod report, mapped onto java-tron's on-chain TRC10 exchange withdrawal path.

### Impact Explanation
An attacker with modest capital (or a colluding block producer) can manipulate the pool ratio around a victim's `ExchangeWithdrawContract` execution to extract value from the victim, causing the victim to receive materially less of the paired asset than the pool's "fair" ratio would imply immediately before the attack. This is a direct, reachable loss-of-funds vector for any account that withdraws liquidity through this widely available on-chain contract type.

### Likelihood Explanation
Any account can create an exchange pair, inject liquidity, and any account can call `ExchangeWithdrawContract` — a fully unprivileged, broadcastable transaction. Sandwiching a target's pending withdrawal only requires observing the transaction pool and submitting bracketing `ExchangeTransactionContract`s, a well-known and low-cost MEV technique, making exploitation practically feasible without any special privilege.

### Recommendation
Add a `expected`/minimum-output field to `ExchangeWithdrawContract` (mirroring `ExchangeTransactionContract`'s design) and enforce it in `ExchangeWithdrawActuator.doValidate()`/`execute()`, rejecting the withdrawal if the computed `anotherTokenQuant` (and/or the primary `tokenQuant` side, if partial withdrawal ratios can also shift) falls below the value the caller is willing to accept.

### Proof of Concept
1. Attacker observes a pending `ExchangeWithdrawContract` from victim V for `exchange_id=E`, `token_id=firstTokenID`, `quant=Q`.
2. Attacker submits `ExchangeTransactionContract` selling a large amount of `secondTokenID` into `E` right before V's transaction is packed, skewing `firstTokenBalance`/`secondTokenBalance` so the Bancor ratio computed in `ExchangeWithdrawActuator.execute()` (lines 74-89) yields a much smaller `anotherTokenQuant` for V.
3. V's `ExchangeWithdrawContract` executes with no way to reject the unfavorable ratio, since no minimum-output field exists on the contract, and V is credited the reduced `anotherTokenQuant`.
4. Attacker reverses their trade afterward, restoring the pool and pocketing the difference extracted from V.

### Citations

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L24-29)
```text
message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L31-37)
```text
message ExchangeTransactionContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
  int64 expected = 5;
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L68-104)
```java
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
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L132-180)
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

```
