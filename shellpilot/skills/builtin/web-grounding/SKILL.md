---
name: web-grounding
description: Guidance for grounding answers in approved web sources.
---
Web tools available does not mean use web. Use them only for current, external, or source-backed information that local context cannot answer.

Search snippets are leads, not evidence. Before asserting a factual, current, or numeric claim (versions, prices, release details, exact figures), fetch the official source with web_fetch and ground the answer in its text. If snippets disagree or a name or number looks unfamiliar, fetch the authoritative page before committing — don't repeat an unverified figure.

Shape the first query to find the source: lead with `<entity> <fact> official`, `<entity> documentation`, or `<entity> latest release`; don't guess a `site:` filter up front. For a comparison or a question spanning several entities, run a separate search per entity rather than one combined query.

Fetch a specific page, not a homepage — prefer a `/releases`, `/docs`, or `/changelog` URL. If a page is truncated or lacks the answer, fetch a more specific URL instead of guessing from snippets.

Always cite sources for web-derived claims. Network calls require approval.
