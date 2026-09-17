Confirmed: both `ExchangeInjectActuator` and `ExchangeWithdrawActuator` are restricted to the exchange's creator, but that creator is still an unprivileged, anonymous account (anyone can call `ExchangeCreateContract` and become a creator), and both actuators compute the counter-token amount from the live pool ratio at execution time with **no user-supplied minimum/maximum bound**, unlike `ExchangeTransactionContract` which carries an `expected` field checked in `ExchangeTransactionActuator`.

### Title
Missing slippage/expected-amount protection in `ExchangeInjectContract` and `ExchangeWithdrawContract` allows fund loss via trade front-running - ([File: actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java], [File: actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java])

### Summary
`ExchangeInjectContract` and `ExchangeWithdrawContract` compute the counter-token amount for a liquidity add/remove operation from the exchange pool's *current* balances at execution time, with no user-supplied minimum-received or maximum-paid bound. Unlike `ExchangeTransactionContract`, which includes an `expected` field enforced in `ExchangeTransactionActuator.doValidate()` [1](#0-0) , `ExchangeInjectContract` and `ExchangeWithdrawContract` have no analogous field [2](#0-1) .

### Finding Description
`ExchangeInjectActuator.execute()` derives `anotherTokenQuant` from the live `firstTokenBalance`/`secondTokenBalance` ratio at the moment the transaction is applied [3](#0-2) . `ExchangeWithdrawActuator.execute()` behaves the same way for withdrawals [4](#0-3) . Neither actuator's `doValidate()` checks `anotherTokenQuant` against any caller-supplied bound; `doValidate()` in `ExchangeInjectActuator`/`ExchangeWithdrawActuator` only checks balance sufficiency and creator identity, never comparing the computed counter-amount to an expected value [5](#0-4) .

Any account can become an exchange "creator" by broadcasting an `ExchangeCreateContract` transaction (an unprivileged action) and later needs to inject/withdraw liquidity [6](#0-5) . Because the pool ratio is a shared, mutable on-chain state that any other unprivileged account can move via a broadcast `ExchangeTransactionContract` trade, an attacker can observe a pending Inject/Withdraw transaction in the mempool and front-run it with a trade that shifts the ratio, causing:
- Inject: the victim pays far more of the second token than the ratio implied when they signed the transaction.
- Withdraw: the victim receives far less of the second token than expected for the `tokenQuant` they are giving up.

This is the same root cause pattern as the referenced Notional bug: a value-computing operation against a mutable price/ratio with no caller-enforced bound, executed atomically at transaction-apply time, is exploitable by transaction ordering (front-running) between broadcast time and block-inclusion time.

### Impact Explanation
A victim's `ExchangeInjectContract`/`ExchangeWithdrawContract` transaction can be sandwiched or simply preceded by an attacker's `ExchangeTransactionContract` trade against the same `exchange_id`, causing the victim to lose funds relative to the ratio they observed when constructing/signing their transaction — a direct loss of value for the broadcasting account with no way to bound the loss, since `ExchangeInjectContract`/`ExchangeWithdrawContract` carry no `expected` field to reject an unfavorable execution.

### Likelihood Explanation
Any account can trigger the front-running trade by simply broadcasting an `ExchangeTransactionContract` (fully permissionless, no special role needed), and mempool transaction ordering/timing is easily observable and influenced by any broadcaster. The only precondition on the victim side is that they must be the exchange creator, but exchange creation itself is unprivileged and open to any account [7](#0-6) .

### Recommendation
Add an `expected`-style bound to `ExchangeInjectContract` and `ExchangeWithdrawContract` (e.g., a max-second-token-cost for Inject and a min-second-token-received for Withdraw), and enforce it in `ExchangeInjectActuator.doValidate()` / `ExchangeWithdrawActuator.doValidate()` the same way `ExchangeTransactionActuator` enforces `tokenExpected` against `anotherTokenQuant` [1](#0-0) .

### Proof of Concept
1. Attacker (or any user) monitors the mempool for a pending `ExchangeWithdrawContract` from the creator of exchange `X` (or `ExchangeInjectContract`).
2. Attacker broadcasts an `ExchangeTransactionContract` trade on exchange `X` with an aggressive `quant` that shifts `firstTokenBalance`/`secondTokenBalance` significantly, timed to land in the same block before the victim's transaction (standard front-running via gas/energy/fee ordering or same-block ordering).
3. The victim's Inject/Withdraw is applied afterward against the now-shifted ratio in `ExchangeInjectActuator.execute()` / `ExchangeWithdrawActuator.execute()`, computing an `anotherTokenQuant` far from what the victim expected — with `doValidate()` performing no rejection since no `expected` bound exists in the contract message.
4. Victim's transaction still succeeds (all existing checks pass) but transfers value at an unfavorable, manipulated ratio, realizing an unbounded loss relative to the state seen when the transaction was constructed.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L217-221)
```java
    long anotherTokenQuant = exchangeCapsule.transaction(tokenID, tokenQuant,
        dynamicStore.allowStrictMath(), allowHarden());
    if (anotherTokenQuant < tokenExpected) {
      throw new ContractValidateException("token required must greater than expected");
    }
```

**File:** protocol/src/main/protos/core/contract/exchange_contract.proto (L17-29)
```text
message ExchangeInjectContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}

message ExchangeWithdrawContract {
  bytes owner_address = 1;
  int64 exchange_id = 2;
  bytes token_id = 3;
  int64 quant = 4;
}
```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeInjectActuator.java (L71-83)
```java
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L181-200)
```java
    if (!accountCapsule.getAddress().equals(exchangeCapsule.getCreatorAddress())) {
      throw new ContractValidateException("account[" + readableOwnerAddress + "] is not creator");
    }

    byte[] firstTokenID = exchangeCapsule.getFirstTokenId();
    byte[] secondTokenID = exchangeCapsule.getSecondTokenId();
    long firstTokenBalance = exchangeCapsule.getFirstTokenBalance();
    long secondTokenBalance = exchangeCapsule.getSecondTokenBalance();

    byte[] tokenID = contract.getTokenId().toByteArray();
    long tokenQuant = contract.getQuant();

    long anotherTokenQuant;

    if (dynamicStore.getAllowSameTokenName() == 1
        && !Arrays.equals(tokenID, TRX_SYMBOL_BYTES)
        && !isNumber(tokenID)) {
      throw new ContractValidateException("token id is not a valid number");
    }

```

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeCreateActuator.java (L136-182)
```java
  @Override
  public boolean validate() throws ContractValidateException {
    try {
      return doValidate();
    } catch (ArithmeticException e) {
      throw new ContractValidateException(e.getMessage());
    }
  }

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

```
