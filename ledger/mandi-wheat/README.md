# Mandi wheat claim ledger

Append-only record of khetru's sow-or-wait rain claims for rainfed wheat in
Mandi district. `uv run --all-packages evidence verify --base <ref>` checks
that the ledger only grew since `<ref>`; CI runs it on every push and PR
(`.github/workflows/evidence-verify.yml`).

## Repository rules (owner action)

The owner sets up a GitHub ruleset on `github.com/dsfx3d/khetru`
(Settings → Rules → Rulesets); it cannot be set from code:

- Branch ruleset targeting `main`: block force pushes and block deletion.
- Tag ruleset targeting `mandi-wheat/*`: restrict updates and restrict
  deletions, so a bundle tag can never be moved or removed (R14).

`verify` also fails when a tag recorded in `tags.jsonl` no longer resolves to
its recorded commit, so a moved tag is caught even without the ruleset.
