### Title
Front-running `ExchangeTransaction` can permanently block the exchange creator's `ExchangeWithdraw`/`ExchangeInject` transactions - (File: `actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java`)

### Summary
Any unprivileged account can call `ExchangeTransactionContract` against a TRON bancor-style `Exchange` pool to shift its token-balance ratio. The exchange creator's `ExchangeWithdrawContract`/`ExchangeInjectContract` validations recompute the required counter-token amount and a strict precision/limit check from the *current* pool ratio at validate time. By front-running the creator's transaction with a minimal, low-cost trade, an attacker can shift the ratio just enough to flip these checks from passing to failing, causing the creator's transaction to revert. This can be repeated indefinitely and cheaply, blocking the creator from ever withdrawing or reconfiguring liquidity in the pool — the same "front-run the balance-dependent guard to block a legitimate state-changing transaction" bug class described in the report.

### Finding Description
`ExchangeTransactionActuator` allows **any** account (not just the exchange creator) to trade against an `Exchange`, updating `firstTokenBalance`/`secondTokenBalance` via the bancor formula: [1](#0-0)  — specifically, only address validity, sender balance for the trade, and pool state are checked: [2](#0-1) 

Meanwhile, `ExchangeWithdrawActuator.doValidate()` reads the pool's `firstTokenBalance`/`secondTokenBalance` at validation time, computes `anotherTokenQuant` from that ratio, and enforces a strict precision tolerance and sufficiency checks: [3](#0-2) 
and an analogous balance-sufficiency check exists for the second branch: [4](#0-3) 

Because `ExchangeTransactionContract` can be broadcast by anyone and mutates the exact same shared state (`ExchangeCapsule`'s token balances) that `ExchangeWithdrawActuator`/`ExchangeInjectActuator` validate against, an attacker can:
1. Observe the creator's pending `ExchangeWithdrawContract` (or `ExchangeInjectContract`) transaction in the mempool.
2. Front-run it with a dust-sized `ExchangeTransactionContract` trade (as small as a few units of the traded token, well within `getExchangeBalanceLimit()`), shifting the ratio.
3. Cause the creator's transaction to fail either the "Not precise enough" rounding check or the "exchange balance is not enough" / "the calculated token quant must be greater than 0" checks in `ExchangeWithdrawActuator`/`ExchangeInjectActuator`, since these depend entirely on the pool ratio at validation time.

This mirrors the reported `VaultBooster.setBoost()` issue: a balance/ratio precondition, satisfiable at transaction-construction time, is invalidated by an unprivileged actor's front-running transaction against the same shared, permissionlessly-writable state, causing the legitimate owner/creator's transaction to revert.

### Impact Explanation
The exchange creator can be perpetually blocked from withdrawing (or injecting/reconfiguring) liquidity from/to their own `Exchange` pool, since any withdraw/inject attempt can be repeatedly front-run with negligible-cost trades. Because `Exchange` pools hold real TRX/TRC-10 asset balances contributed by the creator, this results in **de-facto permanent freezing of the creator's funds/liquidity** in the pool — the creator can never successfully execute a withdrawal as long as an attacker chooses to keep front-running, at essentially zero cost to the attacker (dust trade amounts, and `calcFee()` for these actuators is `0`).

### Likelihood Explanation
`ExchangeTransactionContract` is a normal user-facing contract type with no special permission requirements beyond holding a small amount of the traded token, and exchange IDs/state are public. Front-running is straightforward on TRON given transactions are visible pre-confirmation, and the attack costs are negligible (dust-sized trades, zero actuator fee). This makes the griefing pattern easy and cheap to execute repeatedly.

### Recommendation
- Do not compute a strict-precision or exact-ratio check purely from spot pool state at validate time; instead, allow the withdraw/inject caller to specify a slippage-tolerant bound (e.g., min/max acceptable counter-token amount), similar to how `ExchangeTransactionContract` already has a `tokenExpected` slippage parameter.
- Alternatively, allow the exchange creator to pause/withdraw priority or use a commit-reveal / time-locked configuration approach that is immune to same-block ratio manipulation by third parties.

### Proof of Concept
1. Creator deploys/owns `Exchange` pool with `firstToken`/`secondToken` balances such that a planned `ExchangeWithdrawContract` (with a fixed `quant`) currently satisfies the precision check in `ExchangeWithdrawActuator.doValidate()`.
2. Attacker observes the creator's pending withdraw transaction in the mempool.
3. Attacker submits a minimal `ExchangeTransactionContract` trade against the same `exchangeId` (any account can do this per `ExchangeTransactionActuator.doValidate()`, lines 119-224, which contains no creator-only restriction), which is included first and updates `firstTokenBalance`/`secondTokenBalance`.
4. The creator's `ExchangeWithdrawContract` is then processed against the new ratio; the recomputed `anotherTokenQuant` and precision `remainder` check in `ExchangeWithdrawActuator.doValidate()` (lines 214-271) now fails, reverting with "Not precise enough" or "exchange balance is not enough".
5. Attacker repeats this for every subsequent withdraw attempt at negligible cost, indefinitely preventing the creator from withdrawing pool funds.

### Citations

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeTransactionActuator.java (L142-166)
```java
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

    ExchangeCapsule exchangeCapsule;
    try {
      exchangeCapsule = Commons.getExchangeStoreFinal(dynamicStore, exchangeStore, exchangeV2Store)
          .get(ByteArray.fromLong(contract.getExchangeId()));
    } catch (ItemNotFoundException ex) {
      throw new ContractValidateException("Exchange[" + contract.getExchangeId()
          + ActuatorConstant.NOT_EXIST_STR);
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

**File:** actuator/src/main/java/org/tron/core/actuator/ExchangeWithdrawActuator.java (L245-271)
```java
    } else {
      anotherTokenQuant = bigFirstTokenBalance.multiply(bigTokenQuant)
          .divideToIntegralValue(bigSecondTokenBalance).longValueExact();
      if (secondTokenBalance < tokenQuant || firstTokenBalance < anotherTokenQuant) {
        throw new ContractValidateException("exchange balance is not enough");
      }

      if (anotherTokenQuant <= 0) {
        throw new ContractValidateException("withdraw another token quant must greater than zero");
      }

      if (allowHarden) {
        BigDecimal remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, RoundingMode.HALF_UP)
            .subtract(BigDecimal.valueOf(anotherTokenQuant));
        if (remainder.compareTo(
            BigDecimal.valueOf(anotherTokenQuant).multiply(new BigDecimal("0.0001"))) > 0) {
          throw new ContractValidateException("Not precise enough");
        }
      } else {
        double remainder = bigFirstTokenBalance.multiply(bigTokenQuant)
            .divide(bigSecondTokenBalance, 4, BigDecimal.ROUND_HALF_UP).doubleValue()
            - anotherTokenQuant;
        if (remainder / anotherTokenQuant > 0.0001) {
          throw new ContractValidateException("Not precise enough");
        }
      }
```
