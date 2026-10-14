# Engineering Contribution Memory (UC-10)

A Gravity monitor agent (gravity-cli 0.5.4, plain LangGraph). Read-only: it never writes to GitHub, Jira, Slack or docs.

Reads your last 6 months of work, links PRs, tickets, discussions and docs into projects, finds work that never made it into a ticket, and keeps an evidence-linked profile. Code computes every number; the model (optional) only labels and words, and its output is validated against real item ids.

- `src/normalize.py`, `linking.py`, `metrics.py`, `guards.py`, `ledger.py`, `weekly.py`: pure logic, unit tested
- `src/sources.py`: source adapters (real gateway tools, fixtures for tests); failed or missing sources are named in the result
- `src/store.py`: full contribution history in Gravity's `get_store()` (items, seen ids, verdicts); `state.memory` keeps only a small marker (backfill_done, cursors, last run, gap statuses). Falls back to `state.memory` alone when no store exists.
- `src/adapters.py`: read-only Jira, Confluence, Notion, Google Drive and Slack-search adapters.
- `src/embed.py`: embeddings through Gravity's LLM endpoint (voyageai/voyage-4-lite); blended with keyword (TF-IDF) similarity for linking and grouping, keywords alone if the call fails.
- `src/agent.py`: LangGraph pipeline, `async graph()`
- `tests/`: our own fixture built from the spec text (not Gravity's samples)

Run tests: `uv venv --python 3.12 && uv pip install -r requirements.txt pytest && pytest`

Status: work in progress, private, not submitted to Gravity.

## On-demand requests
This agent is `kind: monitor`. The guide offers follow-up runs with the user's words (`inputs["message"]`) only for `kind: interactive`, so a monitor does not get them. On-demand answers (what did I do on project X, year summary, review-pack, promotion evidence) are chosen with the `request` dropdown plus `period_from` / `period_to` on a manual run. "What to produce" is the only output selector and the two date fields are the only source of a review period: there is no typed-request box, and no text is read for a mode or dates. They are answered from the saved profile with no source reads. Free-text follow-ups are not supported; this is a deliberate choice, not an oversight.

## Required inputs and example run
Required form inputs: `repos_or_org` and `role_and_level`. Jira projects, Slack channels and docs are optional. `github_username` and `jira_site` are optional overrides, filled from the connected accounts when blank. `timezone` defaults to Asia/Kolkata. Optional: `include_dms` (No), `docs_source`, `backfill_months` (6), `review_framework`. Jira uses jira.search_issues (assigned, created or commented by you, in `jira_projects`), jira.get_issue with the changelog (reopens, moved keys) and jira.list_issue_comments. `docs_source` picks Confluence (cql_search, get_page_by_id), Notion (search_notion_page, get_page_markdown) and/or Google Drive (find_file, get_document and revision edit evidence). A source that is not chosen or fails is named in the result, not silently skipped. Slack uses slack.search_messages for your own messages in the chosen channels only, then fetches each thread; DMs are read only with `include_dms` = Yes. Run `gravity connect` first to check whose accounts the gateway uses. On a personal/per-builder route, connect each needed app separately, for example `gravity connect github`, `gravity connect jira` and `gravity connect slack`. If it reports `builder:local` or a shared test account, do not connect personal apps there; ask Gravity to reconnect the test account.
```
gravity run --input github_username='USER' --input repos_or_org='owner/repo' --input jira_site='https://example.atlassian.net' --input jira_projects='ABC' --input slack_channels='#general' --input role_and_level='Software Engineer' --input backfill_months='6'
gravity run --input github_username='USER' --input repos_or_org='owner/repo' --input jira_site='https://example.atlassian.net' --input jira_projects='ABC' --input slack_channels='#general' --input role_and_level='Software Engineer' --input request='one_on_one'
```
Slack is read only for the channels named in `slack_channels`. Commits by the user are read too (via github.list_commits and github.get_commit), so repos without PRs still show work. Commits already counted through a PR, merge commits and bot commits are dropped; a commit that a later "This reverts commit" points at is listed but not counted. The second command answers from saved memory (`.gravity/memory.json`) even if the first run found nothing.


## Submission hygiene

`gravity validate` resolves requirements.lock for Python 3.12/Linux ARM64. Keep that lockfile with the source. `gravity build` packages manifest, requirements, src and tests; it does not package audit/, .gravity/, out/ or root run_all.py. Inspect the built ZIP before publishing.

Use `gravity dev` or a scoped `gravity run` for a deliberate live test. These call real tools and models. Selecting Google Drive can read the connected account's edited documents in the run window; it is not restricted to a synthetic test document.

`run_all.py` is a personal synthetic live-test harness retained because its output checker is covered by a regression test. It has fixed test scopes and moves local saved state to a backup before running. Do not use it as the normal agent entry point or run it casually.

Local fixture tests are not Gravity's official UC-10 acceptance suite. Interview evidence URLs may reveal internal names and are not safe to share externally as-is. Keep this repository private: removing audit artifacts from the current tree does not erase them from earlier Git commits.


## Review onboarding branch (offline implementation)

The revised first run can use GitHub only. Role_and_level and explicit owner/repo selections are required for initial setup; GitHub username is read from the connected account if blank and request defaults to weekly_update. Jira projects, Slack channels and docs are optional. Missing/unselected sources are named once in a plain Not covered line, not disguised as successful full coverage. Invalid explicit source selections still stop reads rather than being silently substituted.

Select one or more explicit owner/repo entries, separated by commas or newlines. Blank input and bare account/org names stop before any contribution read. Automatic account/org discovery is disabled. Only selected repositories are read; exclusions apply before reads. When the selection changes, only those repositories backfill the requested window (six months by default). Previously saved outside-scope GitHub evidence, seen IDs and cursors remain saved but are excluded from analysis and all output views. Optional Jira/Slack/docs scopes remain separate. Saved-only outputs do not fetch contributions. This intentionally differs from the reviewer's automatic discovery request and must be flagged before submission. Dates use timezone; schedule remains Friday17:00IST.

Source selections are saved in one shared setup, independent of the code version. Later runs and old schedules use it, not stale source fields. Multiple explicit owner/repo selections are supported; account/org discovery is disabled. Each scope has its own successful sync cursor. Failed/capped reads do not advance it, and successful scopes are not refetched when enriching a new source.

Choose the report you want and enter any new Jira projects, Slack channels or docs sources. The setup saves behind the scenes and the report adds missing history automatically. Blank fields keep saved sources. The optional Stop reading sources field disables named sources explicitly; saved evidence remains archived. No setup revision, save action or enrichment action is shown.

Each on-demand run shares one 10-model-call / 2-minute limit across reading missing history and writing the requested report. New history is read in bounded windows, with successful coverage saved for later runs. A partial report says that its coverage is incomplete; later reports resume unfinished scopes without retyping. Existing completed scopes are not reread for an on-demand report. The initial profile and weekly update retain their own budgets. Missing/corrupt stored history stops instead of silently resetting it. No hidden background run is started.

The application uses the same saved-state namespace across code upgrades of the same agent. This does not prove hosted Gravity retains that state on an upgrade; hosted continuity is unverified. A different agent is not a continuation of the old one. Real read failures remain separate diagnostics. Interview-output scope text is redacted consistently.

Status: local source-snapshot Git branch only, based on verified cleanup head0f6b442. Not pushed or published, original history not preserved in this local base. Automated tests use fixtures/mocks, not the official samples/UC-10 pack. Two fresh-version real run IDs, model-credit availability and full official acceptance remain unverified. After publication, the reviewer recommends a fresh/incognito thread due to a reported pinned-version bug; verify actual version and run IDs.
