# Q194: Owner or Capability Confusion in burn

## Question
Can `aptos_framework::object::burn` be reached with attacker-controlled addresses, refs, seeds, or object IDs that make the module act on an owner, capability, or object context different from the one actually authorized?

## Target
- File/function: aptos-move/framework/aptos-framework/sources/object.move::aptos_framework::object::burn
- Entrypoint: Submit an entry-function transaction invoking `aptos_framework::object::burn` or the direct user flow that reaches it.
- Attacker controls: amounts, recipient addresses, object IDs, property maps, collection and token names, seeds, beneficiaries, operator choices, and lockup-related inputs
- Exploit idea: Exploit any confusion between the caller, the referenced object or capability, and the resource or asset that is later mutated.
- Invariant to test: The authorized caller, referenced capability or object, and mutated asset owner must all refer to the same intended context.
- Expected Immunefi impact: High. Unauthorized transaction execution, replay, signer confusion, signature or proof misbinding, or sequence and authorization bypass that lets an unprivileged attacker cause state transitions on behalf of another user or module context.
- Fast validation: Write a Move or e2e test that mixes caller, object, and ref contexts and assert the entry function cannot mutate any unauthorized target.
