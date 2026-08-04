# cadloop — notes for agents

## Review loop (Tyrekick)

`docs/walkthrough/pen-holes/index.html` carries a Tyrekick widget, so reviewers
can pin comments on the page and an agent can read them back here.

| | |
| --- | --- |
| project slug | `cadloop-pen-holes` |
| review URL | `https://cadloop-pen-holes.pages.dev` (Cloudflare Pages project `cadloop-pen-holes`) |
| destination | `https://tyrekick-cadloop-pen-holes.richard-fortune.workers.dev` |
| transport | json (Cloudflare Worker, no Discord mirror) |
| worker | `tyrekick-cadloop-pen-holes`, source in `tools/tyrekick-worker/` |
| KV namespace | `cadloop-pen-holes-FEEDBACK` |
| management token | Cloudflare secret `TYREKICK_TOKEN` on that worker — never in this repo |

The read-back is registered as the `tyrekick` MCP server for this project, so
"list the open feedback and fix what people flagged" works in any session here.

Re-deploying the page after an edit:

```console
npx wrangler pages deploy docs/walkthrough/pen-holes \
  --project-name cadloop-pen-holes --branch main
```

Do not point this page at a different destination. If another part of the repo
needs reviewing, give it its own slug and its own worker rather than sharing
this store.
