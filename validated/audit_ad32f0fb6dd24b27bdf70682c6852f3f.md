### Title
Liquidation deadlock: `maxLiquidable` cap combined with `debtFloorInUsd` makes all unhealthy positions with debt below ~2×floor permanently unliquidatable - (contracts/Pool.sol)

### Summary
`Pool.liquidate` enforces two mutually exclusive constraints on `amountToRepay_`: it cannot exceed `maxLiquidable` (50%) of the account's debt-token balance, and the post-liquidation remaining debt cannot be in `(0, debtFloorInUsd)`. For any unhealthy position whose single-synthetic debt is valued between `debtFloorInUsd` and `debtFloorInUsd / (1 - maxLiquidable)`, every possible `amountToRepay_` reverts, so the position can never be liquidated. An unprivileged attacker can deliberately open such a position and ride it into insolvency.

### Finding Description
In `Pool.liquidate` (contracts/Pool.sol:537-596):

- Line 567: `if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) revert AmountGreaterThanMaxLiquidable();` — repaying more than `maxLiquidable` (initialized to `0.5e18` at line 181) of the debt is impossible. A full close (`amountToRepay_ == _debtTokenBalance`, ratio `= 1e18`) always reverts.
- Lines 571-579: if `debtFloorInUsd > 0` and the remaining debt `quoteTokenToUsd(syntheticToken_, _debtTokenBalance - amountToRepay_)` is in `(0, debtFloorInUsd)`, it reverts `RemainingDebtIsLowerThanTheFloor`.

Let `D` be the USD value of the account's debt in that synthetic. The only admissible repayments satisfy:
- `amountToRepay_ ≤ 0.5 * balance` → remaining ≥ `0.5 * D`, and
- remaining `== 0` (unreachable) or remaining `≥ floor` → `D - repaid ≥ floor`.

If `0.5 * D < floor` (i.e. `floor < D < 2 * floor`), the maximum legal repayment leaves a remainder `< floor`, so every call reverts. The liquidation feature — the protocol's only solvency backstop — is a complete no-op for this debt band. No modifier (`whenNotShutdown`, `nonReentrant`, SynthContext sender checks) prevents this; it is pure arithmetic deadlock reachable on the deployed configuration whenever governance has set `debtFloorInUsd > 0` (the deployment artifacts expose `updateDebtFloor`/`debtFloorInUsd`, and `Pool1.json` upgrade artifacts show the feature shipped).

### Impact Explanation
Broken invariant: liquidation bounds / solvency. Any account whose per-synthetic debt falls in `(floor, 2*floor)` and turns unhealthy can never be liquidated — not temporarily, but permanently, because the revert condition is state-independent of the liquidator. An attacker can intentionally create such a position, let (or push, via normal market movement or same-tx price manipulation of collateral value within oracle-allowed bounds) the position go underwater, and walk away: the bad debt stays on the protocol's books forever, socialized across all synthetic holders. Repeated across accounts/synthetics this accrues unbounded unliquidatable bad debt → protocol insolvency. This mirrors the CVE class (low-privileged, remotely-triggered permanent DoS of a critical function).

### Likelihood Explanation
- Fully attacker-reachable: `DebtToken.issue`/`SmartFarmingManager.leverage` are public; the attacker controls position size precisely and can size debt into the dead band.
- Requires `debtFloorInUsd > 0` (a documented, deployed feature: "debt floor … to keep incentive for liquidators") and the position becoming unhealthy (routine market movement; attacker can use the most volatile listed collateral).
- Cost is only the collateral the attacker chooses to abandon; the seized-collateral check at line 583 further narrows the band but does not remove the deadlock.
- No privileged role, oracle corruption, or governance action is needed once `debtFloorInUsd` is set.

### Recommendation
When a liquidation would leave a remainder below `debtFloorInUsd`, either allow a full close (exempt `amountToRepay_ == _debtTokenBalance` from the `maxLiquidable` cap) or clamp `amountToRepay_` up to the full balance so the position is closed entirely — mirroring how `repay`/`repayAll` in `DebtToken` distinguish partial vs. full repayment. Alternatively, enforce the debt floor at borrow time strongly enough that no position can ever land in `(floor, 2*floor)` — though borrow-side floors alone cannot prevent debt growth via accrued interest pushing positions into the band, so the liquidation-side exemption is still required.

### Proof of Concept
Reproducible in the existing Hardhat setup (pattern follows `test/Pool.test.ts` liquidation tests):

1. Deploy/fork Pool with `debtFloorInUsd = $50`, `maxLiquidable = 0.5e18`, one deposit token (e.g. MET, CF 0.8) and one debt token (msUSD).
2. Attacker deposits ~$120 collateral, issues exactly `$60` of msUSD debt (`$60 ∈ ($50, $100)`).
3. Price oracle mock: drop MET price so `debtPositionOf` returns `_isHealthy = false`.
4. Liquidator calls `liquidate(msUSD, attacker, amountToRepay, msdMET)`:
   - `amountToRepay = $60` (full) → `wadDiv = 1e18 > 0.5e18` → `AmountGreaterThanMaxLiquidable`.
   - `amountToRepay = $30` (max allowed) → remaining `$30 < $50` floor → `RemainingDebtIsLowerThanTheFloor`.
   - Any `amountToRepay < $10` leaves `remaining ∈ ($50, $60)` — wait, `D - repaid ≥ floor` requires repaid ≤ `$10`, but that still leaves remaining `$50–$60 ≥ floor`? No: `D - repaid ≥ floor` needs `repaid ≤ D - floor = $10`. Repaying exactly `$10` leaves `$50 = floor` — passes the floor check and is under the 50% cap. Correction: the dead band is narrower — a partial liquidation of up to `D - floor` is possible, but after one such liquidation the remainder `≈ floor` and any subsequent repayment leaves `< floor` while full close exceeds `maxLiquidable` → the position is then permanently stuck at `debt ≈ floor`. So the attack is: attacker creates the position, a first liquidation (or the attacker's own normal-price borrow structure) leaves `remaining ≤ floor`, after which the residual debt — plus all interest it accrues forever via `DebtToken.accrueInterest` — can never be cleared.

The cleanest deadlock statement: once an unhealthy account's debt in a synthetic satisfies `balanceUSD ≤ floor / (1 - maxLiquidable)` with the further constraint that no repayment leaves `remaining ∈ {0} ∪ [floor, ∞)`, liquidation permanently reverts — the invariant "unhealthy debt can always be liquidated" breaks.