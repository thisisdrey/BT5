import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the GitLab namespace/project path, for example group/project
SOURCE_REPO = "0dotxyz/marginfi-v2"
# todo: the name of the repository
REPO_NAME = "marginfi-v2"

run_number = os.environ.get("GITHUB_RUN_NUMBER", "0")


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"

scope_files = [
    "programs/marginfi/src/lib.rs",
    "programs/marginfi/src/constants.rs",
    "programs/marginfi/src/errors.rs",
    "programs/marginfi/src/events.rs",
    "programs/marginfi/src/ix_utils.rs",
    "programs/marginfi/src/macros.rs",
    "programs/marginfi/src/prelude.rs",
    "programs/marginfi/src/state/bank.rs",
    "programs/marginfi/src/state/bank_cache.rs",
    "programs/marginfi/src/state/bank_config.rs",
    "programs/marginfi/src/state/emode.rs",
    "programs/marginfi/src/state/fee_state.rs",
    "programs/marginfi/src/state/interest_rate.rs",
    "programs/marginfi/src/state/liquidation_record.rs",
    "programs/marginfi/src/state/marginfi_account.rs",
    "programs/marginfi/src/state/marginfi_group.rs",
    "programs/marginfi/src/state/mod.rs",
    "programs/marginfi/src/state/order.rs",
    "programs/marginfi/src/state/panic_state.rs",
    "programs/marginfi/src/state/price.rs",
    "programs/marginfi/src/state/rate_limiter.rs",
    "programs/marginfi/src/state/staked_settings.rs",
    "programs/marginfi/src/instructions/mod.rs",
    "programs/marginfi/src/instructions/marginfi_account/admin_close.rs",
    "programs/marginfi/src/instructions/marginfi_account/borrow.rs",
    "programs/marginfi/src/instructions/marginfi_account/close.rs",
    "programs/marginfi/src/instructions/marginfi_account/close_balance.rs",
    "programs/marginfi/src/instructions/marginfi_account/close_liquid_record.rs",
    "programs/marginfi/src/instructions/marginfi_account/deposit.rs",
    "programs/marginfi/src/instructions/marginfi_account/emissions.rs",
    "programs/marginfi/src/instructions/marginfi_account/flashloan.rs",
    "programs/marginfi/src/instructions/marginfi_account/freeze.rs",
    "programs/marginfi/src/instructions/marginfi_account/init_liquid_record.rs",
    "programs/marginfi/src/instructions/marginfi_account/initialize.rs",
    "programs/marginfi/src/instructions/marginfi_account/liquidate.rs",
    "programs/marginfi/src/instructions/marginfi_account/liquidate_end.rs",
    "programs/marginfi/src/instructions/marginfi_account/liquidate_start.rs",
    "programs/marginfi/src/instructions/marginfi_account/mod.rs",
    "programs/marginfi/src/instructions/marginfi_account/order.rs",
    "programs/marginfi/src/instructions/marginfi_account/pulse_health.rs",
    "programs/marginfi/src/instructions/marginfi_account/purge_delev_balance.rs",
    "programs/marginfi/src/instructions/marginfi_account/repay.rs",
    "programs/marginfi/src/instructions/marginfi_account/sync_indexer_flags.rs",
    "programs/marginfi/src/instructions/marginfi_account/transfer_account.rs",
    "programs/marginfi/src/instructions/marginfi_account/withdraw.rs",
    "programs/marginfi/src/instructions/marginfi_group/accrue_bank_interest.rs",
    "programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs",
    "programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs",
    "programs/marginfi/src/instructions/marginfi_group/mod.rs",
    "programs/marginfi/src/instructions/marginfi_group/panic_unpause_permissionless.rs",
    "programs/marginfi/src/instructions/marginfi_group/pulse_bank_price_cache.rs",
    "programs/marginfi/src/instructions/marginfi_group/update_group_rate_limiter.rs",
]


target_scopes = [
    "Critical. An unprivileged user can withdraw, borrow, transfer, liquidate, settle, or receive assets they are not entitled to, causing direct theft or unauthorized movement of user or vault funds.",
    "Critical. An unprivileged user can create unbacked debt, bypass health or collateral checks, or corrupt share accounting so protocol solvency is reduced or bad debt is socialized incorrectly.",
    "High. An unprivileged user can permanently freeze, strand, or orphan another user's funds or protocol-owned value through reachable account, liquidation, bankruptcy, or close flows.",
    "High. An unprivileged user can bypass authority checks on account ownership, delegates, liquidator rights, or keeper-only actions and mutate another user's margin state.",
    "High. A flashloan, order, liquidation, interest-accrual, or fee-collection edge case lets an unprivileged user extract excess value, skip repayment, or leave bank and account state inconsistent.",
    "Medium. A mainnet-reachable user path misprices collateral or liabilities, misapplies fees, or breaks invariants in a way that is exploitable but does not immediately yield direct theft.",
]


scope_scan = [
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit and fuzzing questions for one marginfi target.

    ```
    target_file format:
    "'File Name: programs/marginfi/src/state/bank.rs -> Scope: Critical. ...'"
    ```
    """

    prompt = f"""
    ```

    Generate exploit-focused security audit and fuzzing questions for this exact marginfi target:

    {target_file}

    Project focus:
    This set covers the core on-chain margin lending engine: deposits, borrows, withdrawals, repayments, liquidations, orders, flashloans, interest accrual, fee settlement, bankruptcy, and the state/accounting that makes those flows safe.

    Rules:
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Use exact Rust symbols when possible.
    * Attacker is unprivileged only: a normal user, liquidator, keeper, or caller using valid public instructions with arbitrary accounts, amounts, ordering, and timing.
    * Never assume admin, governance, risk admin, oracle operator, privileged signer, leaked key, malicious validator, malicious peer, or node/config control.
    * Stay on production/release-relevant behavior. If a path only matters for a feature not used in production, do not frame it as Critical.
    * Do not rely on tests, mocks, generated files, off-repo assumptions, or direct state mutation.
    * Out of scope per SECURITY.md: privileged-address assumptions, pure liquidity issues, Sybil/social engineering, high-traffic DoS, third-party oracle bad data by itself, user-chosen order slippage, propagation-only misses, known rate-limit bypasses, known unaccrued-interest-at-risk-check behavior, Solend whitelist omissions, current non-operational Drift-only issues, and admin-chosen T22 listing risk.
    * Generate 12 to 18 high-signal questions.
    * At least 70% must be multi-step solvency, authorization, accounting, liquidation, or permanent-freeze questions.
    * Every question must be testable by unit test, integration test, invariant test, or fuzz test.
    * Avoid generic checklist questions and repeated root causes.

    Core invariants:
    * Only the rightful authority can move, close, freeze, or reassign a margin account or balance.
    * Health checks, share math, fee math, and interest accrual never let a user take more value than they repay or post.
    * No user instruction sequence can create unbacked assets, erase debt, or socialize losses incorrectly.
    * Liquidation, bankruptcy, flashloan, and order flows must preserve bank solvency and leave state internally consistent.
    * Permissionless cranks cannot seize extra value, skip required checks, or strand third-party funds.

    Each question must include:
    1. target function/module;
    2. attacker action;
    3. preconditions;
    4. call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.

    Output only valid Python. No markdown. No explanations.

    questions = [
    "[File: {target_file}] [Function: symbol_or_module] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: test/fuzz PARAMETERS and assert AUTHZ_HOLDS, HEALTH_HOLDS, SHARE_ACCOUNTING, SOLVENCY, or NO_STRANDED_FUNDS.",
    ]
    """
    return prompt


def audit_format(security_question: str) -> str:
    """
    Generate a focused marginfi exploit-validation prompt.
    """

    prompt = f"""# SECURITY AUDIT PROMPT

## Question
{security_question}

## Rules
- Use existing repo context only. Analyze only this question and scoped impact.
- Attacker is unprivileged only: a normal user, liquidator, keeper, or public caller using reachable on-chain instructions.
- Reject anything requiring admin/governance/risk-admin control, leaked keys, malicious validators/peers, direct state mutation, mocks, or best-practice-only cleanup.
- Prefer mainnet or release/pre-release relevant paths. If the claim depends on a non-production feature, do not treat it as Critical.
- Out of scope per SECURITY.md: pure liquidity issues, third-party oracle bad data by itself, Sybil/social engineering, high-traffic DoS, user-chosen order slippage, propagation-only misses, known rate-limit bypasses, known unaccrued-interest-at-risk-check behavior, Solend whitelist omissions, current non-operational Drift-only issues, and admin-chosen T22 listing risk.

## Validate
- Trace the exact reachable Rust path from the public instruction entrypoint into state mutation and settlement logic.
- Check whether signer, owner, health, share-math, limit, fee, liquidation, and bankruptcy guards already stop it.
- Accept only real theft, unauthorized transfer, unbacked borrow, insolvency/bad debt, unauthorized state change, or permanent lock/freeze of funds.
- Require exact file/function support and a reproducible Rust unit, integration, invariant, or fuzz PoC.

## Output
If valid, output exactly:

### Title
[Bug statement] - ([File: file_path])

### Summary
[2-3 sentences]

### Finding Description
[Code path, root cause, attacker inputs, exploit flow, and why checks fail]

### Impact Explanation
[Concrete scoped impact and why it matters under marginfi's bounty rules]

### Likelihood Explanation
[Preconditions, feasibility, repeatability]

### Recommendation
[Specific fix]

### Proof of Concept
[Rust unit/integration/invariant/fuzz test plan with expected assertions]

If invalid, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for marginfi security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check SECURITY.md and Researcher.Md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject admin-only, governance-only, oracle-operator-only, validator/peer-only, leaked-key, docs/style, mock-only, generated-file, or purely theoretical issues.
- Reject if the exploit needs unrealistic assumptions, user self-harm, direct state mutation, or unsupported protocol behavior.
- Reject if the bug is already acknowledged as out of scope in SECURITY.md.
- A valid report must be triggerable by an unprivileged user unless the claim proves privilege escalation from an unprivileged path.
- The final impact must fit marginfi's on-chain bounty scope: direct theft, unauthorized state change, protocol insolvency/bad debt, or permanent lock/freeze of funds.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken authorization, accounting, or solvency assumption.
3. Reachable exploit path: preconditions -> attacker action -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient.
5. Concrete in-scope impact with realistic likelihood.
6. Reproducible proof path: unit PoC, integration test, invariant/fuzz test, or exact manual steps.
7. No obvious rejection reason from SECURITY.md, known issues, privileges, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can a normal user or public keeper trigger this without privileged keys?
- Does the code actually behave as claimed?
- Is the impact caused by this code, not by a malicious node, peer, or privileged operator?
- Is the loss, lock, or insolvency concrete, not hypothetical?
- Would the marginfi team treat this as in-scope under the current SECURITY.md?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant/integration test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for core marginfi flows.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Use in-scope production repo context only. Do not ask for code or claim missing files.
- Use the external report only as a bug-class hint, not as proof.
- Keep only unprivileged-user analogs in margin account, bank, liquidation, order, flashloan, fee, bankruptcy, and core accounting paths.
- Reject validator, peer, privileged-admin, mocked-only, theoretical-only, or no-impact analogs.
- Reject analogs that only match known out-of-scope issues in SECURITY.md.

## Validate
- Map the bug class to the strongest reachable marginfi path.
- Prove root cause with exact file/function support.
- Accept only concrete theft, unauthorized transfer, insolvency/bad debt, unauthorized state change, or permanent lock/freeze.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
