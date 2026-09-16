### Title
Cross-platform floating-point divergence in Bancor swap formula for TRC10 Exchange (x86 `Math.pow` vs ARM `MathWrapper` fallback `StrictMath.pow`) - ([File: chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java])

### Summary
The Defibox incident was caused by an *incompatibility* between two independently-authored contracts that made assumptions about pool state that did not hold, corrupting the swap math and draining funds. Java-tron's own TRC10 "Exchange" module (`ExchangeCreateContract`/`ExchangeInjectContract`/`ExchangeWithdrawContract`/`ExchangeTransactionContract`) has an analogous "incompatibility" root cause, but at the platform level: the Bancor-style relay formula uses `double`-precision `Math.pow`, and the codebase explicitly maintains **separate, non-identical implementations for x86 and ARM** to work around known floating-point non-determinism, gated behind the `ALLOW_STRICT_MATH` chain parameter which defaults to disabled unless a governance proposal has activated it.

### Finding Description
Any signed `ExchangeTransactionContract`, `ExchangeInjectContract`, or `ExchangeWithdrawContract` is executed through `ExchangeCapsule.transaction()`, which — when the hardened path is not active — instantiates `new ExchangeProcessor(supply, useStrictMath)`: [1](#0-0) 

`ExchangeProcessor` computes the Bancor relay conversion using `Maths.pow(base, exponent, useStrictMath)`: [2](#0-1) 

`Maths.pow` dispatches to `StrictMathWrapper.pow` (backed by `StrictMath.pow`) only if `useStrictMath` is true; otherwise it uses the platform-specific `MathWrapper.pow`: [3](#0-2) 

Critically, the two platform builds of `MathWrapper` are **not equivalent**:
- The x86 build always calls `Math.pow(a, b)`: [4](#0-3) 
- The ARM build returns a hardcoded historical value for a fixed lookup table of `(a,b)` pairs seen on mainnet, but falls back to `StrictMath.pow(a, b)` for any pair not already recorded: [5](#0-4) 

So for any new `(a, b)` combination not already present in the ARM cache (i.e., any exchange whose pool balances/quant produce a never-before-seen ratio), an x86 node computes `Math.pow(a,b)` while an ARM node computes `StrictMath.pow(a,b)` for the *same* transaction. These two functions are not contractually guaranteed to be bit-identical, and the whole reason `StrictMathWrapper` and the ARM lookup table exist (per the class Javadoc: "This class is deprecated... for cross-platform consistency, please use `StrictMathWrapper` instead") is that they have historically diverged and caused consensus problems for TRON. `useStrictMath` (i.e., forcing `StrictMath.pow` uniformly on both platforms) is only enabled network-wide once the `ALLOW_STRICT_MATH` proposal has been activated by governance, gated behind fork `VERSION_4_7_7`: [6](#0-5) 

Until that proposal is activated on a given network/chain, an ordinary user can submit an `ExchangeTransactionContract`, `ExchangeInjectContract`, or `ExchangeWithdrawContract` whose pool ratio (`sellTokenBalance`, `buyTokenBalance`, `sellTokenQuant`) has not previously occurred, causing SRs running on different CPU architectures (x86 vs ARM) to compute different `buyTokenQuant`/`anotherTokenQuant` results for the exact same transaction and block. Because these results are written into account balances and into the `ExchangeCapsule`/`ExchangeV2Store` state (part of the state root), this is a state-divergence bug, not merely a display bug — it is the direct analog of the "incompatible contract math" root cause in the Defibox report, expressed instead as an incompatible dual-platform arithmetic implementation.

### Impact Explanation
If validators/witnesses run java-tron on a mix of x86 and ARM hosts (a real-world deployment scenario, since the repository explicitly ships/maintains separate `platform/.../x86` and `platform/.../arm` source sets), any TRC10 Exchange swap transaction that hits an uncached floating-point input can cause nodes to compute different resulting balances for the same block, leading to a **state root mismatch / chain split**, which is explicitly listed as a critical, in-scope impact.

### Likelihood Explanation
This requires: (1) at least one signed transaction to any of the three Exchange actuators using an input combination not already present in the small, hardcoded ARM historical table, and (2) `ALLOW_STRICT_MATH` not yet active on the target chain/fork. Both conditions are plausible: the ARM cache is a finite, historical list of specific mainnet occurrences, so any user-chosen combination not on that exact list will fall through to `StrictMath.pow` on ARM while x86 still always uses `Math.pow`. No privileged role, contract deployment, or special permission is required — only a normal signed exchange contract. Likelihood is Medium because it depends on heterogeneous SR node hardware and pre-`ALLOW_STRICT_MATH` deployment, both of which are plausible but not verifiable from static code alone.

### Recommendation
- Make `ExchangeProcessor` always use `StrictMathWrapper.pow` (i.e., ignore `useStrictMath`/never route through the platform-dependent `MathWrapper`), removing the x86/ARM behavioral divergence entirely rather than gating it behind a governance proposal.
- Alternatively/additionally, replace floating-point `pow` in the exchange relay math with the already-implemented deterministic `BigDecimal`-based `SafeExchangeProcessor` (currently only used when `allowHardenExchangeCalculation` is active) as the unconditional default for all exchange calculations, eliminating platform-dependent floating point from the consensus-critical path.
- Ensure `ALLOW_STRICT_MATH`/harden-exchange proposals are activated by default on all networks before allowing mixed-architecture SR deployments.

### Proof of Concept
Conceptual PoC (cannot be fully executed without live nodes on both architectures):
1. Deploy/observe a network with SR nodes on both x86 and ARM builds of java-tron, with `ALLOW_STRICT_MATH` not yet activated (`dynamicStore.allowStrictMath() == false`).
2. Create an Exchange pair via `ExchangeCreateContract`, then submit an `ExchangeTransactionContract` with `firstTokenBalance`/`secondTokenBalance`/`quant` values chosen so the resulting `(a, b)` argument pair to `Maths.pow` inside `ExchangeProcessor.exchangeToSupply`/`exchangeFromSupply` does not already exist in `platform/arm/.../MathWrapper`'s hardcoded `addPowData` table (e.g., any economically "random" quant, since the table only contains historically-observed mainnet block values).
3. x86 SRs compute the result via `Math.pow`; ARM SRs compute it via `StrictMath.pow` (fallback path in `MathWrapper.pow`'s `getOrDefault`). If these differ in the trailing bits and the difference propagates through `(long) issuedSupply`/`(long) exchangeBalance` truncation to a different integer, the two SR groups persist different account balances / `ExchangeCapsule` state for the same block, producing a state root mismatch. [7](#0-6) [8](#0-7)

### Citations

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeCapsule.java (L124-129)
```java
  public long transaction(byte[] sellTokenID, long sellTokenQuant, boolean useStrictMath,
      boolean hardenedCalc) throws ContractValidateException {
    long supply = 1_000_000_000_000_000_000L;
    Processor processor = hardenedCalc
        ? SafeExchangeProcessor.INSTANCE : new ExchangeProcessor(supply, useStrictMath);

```

**File:** chainbase/src/main/java/org/tron/core/capsule/ExchangeProcessor.java (L17-39)
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
```

**File:** common/src/main/java/org/tron/common/math/Maths.java (L17-19)
```java
  public static double pow(double a, double b, boolean useStrictMath) {
    return useStrictMath ? StrictMathWrapper.pow(a, b) : MathWrapper.pow(a, b);
  }
```

**File:** platform/src/main/java/x86/org/tron/common/math/MathWrapper.java (L9-13)
```java
public class MathWrapper {

  public static double pow(double a, double b) {
    return Math.pow(a, b);
  }
```

**File:** platform/src/main/java/arm/org/tron/common/math/MathWrapper.java (L16-22)
```java
  private static final Map<PowData, Double> powData = Collections.synchronizedMap(new HashMap<>());
  private static final String EXPONENT = "3f40624dd2f1a9fc"; // 1/2000 = 0.0005

  public static double pow(double a, double b) {
    double strictResult = StrictMath.pow(a, b);
    return powData.getOrDefault(new PowData(a, b), strictResult);
  }
```

**File:** actuator/src/main/java/org/tron/core/utils/ProposalUtil.java (L785-799)
```java
      case ALLOW_STRICT_MATH: {
        if (!forkController.pass(ForkBlockVersionEnum.VERSION_4_7_7)) {
          throw new ContractValidateException(
              "Bad chain parameter id [ALLOW_STRICT_MATH]");
        }
        if (dynamicPropertiesStore.allowStrictMath()) {
          throw new ContractValidateException(
              "[ALLOW_STRICT_MATH] has been valid, no need to propose again");
        }
        if (value != 1) {
          throw new ContractValidateException(
              "This value[ALLOW_STRICT_MATH] is only allowed to be 1");
        }
        break;
      }
```
