# Public fixtures and private records

Tests and examples must use invented data chosen for the behavior being tested.
Use short placeholder IDs, fictional merchant and account names, and small toy
amounts. Names alone are not anonymization: do not copy financial amounts,
dates, purchase descriptions, category sets, or relationships from live budgets.
Do not scale, round, or rename an exported household dataset to make a fixture.
Platform names used to exercise integrations are not records of actual purchases.

Keep credentials, personal configuration, browser profiles, reports, and run
journals outside version control. Journals contain private identifiers and amounts.
Savings comparisons are opt-in through private configuration; the application
has no default set of household savings categories.

Before publishing:

1. Review every changed file, including comments, tests, configuration examples,
   screenshots, and generated documentation. Review numerical values and their
   provenance, not just names or credentials.
2. Update the explicit file manifest in `scripts/export_public.py` only for
   reviewed public source files. Exporting selects files; it does not anonymize
   their contents or certify their privacy.
3. Run the tests from the exported directory and review the actual archive.
4. Check all branches, tags, releases, artifacts, and commit history. A clean
   current version does not make earlier versions safe to publish. Never push
   the history of a development checkout containing private records.

If sensitive data was published, restrict access immediately. Git history
rewrites alone do not remove cached commit views or other people's copies;
follow [GitHub's removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
