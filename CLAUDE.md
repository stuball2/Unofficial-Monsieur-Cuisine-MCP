# CLAUDE.md

Unofficial FastMCP server for Monsieur Cuisine (Lidl) recipes. Fork of
`pgreda/Unofficial-Monsieur-Cuisine-MCP`, maintained by Stu (`stuball2`). It is the
recipe half of a kitchen-to-trolley flow: recipes and ingredients from here feed a
separate `sainsburys-mcp` server that fills a Sainsbury's trolley.

Changes usually arrive as feature requests from an **unattended pipeline**
(Dev → QA1 offline pytest → QA2 read-only review → human merge). Nobody is around to
answer questions, so make reasonable choices and say what you assumed in your summary.

## Hard rules

1. **Never touch the live service.** No real calls to `mc-api.tecpal.com`,
   `www.monsieur-cuisine.com` or `accounts.lidl.com`, not in tests and not "just to
   check". Authoring tools write to Stu's **real** account (create/edit/publish/delete).
2. **Tests are fully offline.** `tests/conftest.py` routes all httpx traffic through a
   `respx` router (`mock_http` fixture) that fails on any request you didn't mock,
   blocks raw sockets, and points HOME at a temp dir. Don't weaken or bypass it.
3. **Never read or write the real login session** (`~/.monsieur-cuisine-mcp/`,
   `storage_state.json`). Use the `logged_in` / `logged_out` fixtures instead.
4. **Don't run the login flow or Playwright** (`monsieur-cuisine-mcp-login`). It's
   interactive and needs a human.
5. Every behaviour change ships with tests. Don't delete, skip or `xfail` tests to get
   a green run.
6. Keep each change scoped to its feature request: no drive-by refactors or renames, and
   don't reformat files you're not otherwise editing.

## Commands

```bash
uv sync                          # install (dev deps include pytest, pytest-asyncio, respx)
uv run pytest -q                 # full offline test suite (what QA1 runs, via .agent-pipeline/test.sh)
uv run monsieur-cuisine-mcp      # start the MCP server over stdio (humans only, hits live API)
```

## Layout

- `src/monsieur_cuisine_mcp/server.py`: the FastMCP tool definitions (thin wrappers;
  the docstrings are the tool descriptions the LLM sees, so keep them accurate).
- `src/monsieur_cuisine_mcp/config.py`: URLs, timeouts, paths.
- `src/monsieur_cuisine_mcp/auth.py`: manual browser login and cookie loading.
- `src/monsieur_cuisine_mcp/recipes/`:
  - `client.py`: the authenticated `/proxy-api` call plus error translation (`MCRequestError`).
  - `search.py`: public search, filters and `get_recipe` (falls back to the proxy for your own recipes).
  - `favorites`, `ingredients`, `lifecycle` and `authoring`: account operations.
  - `devices`, `nutrients`, `payloads` and `media`: pure payload builders. These are the easiest to unit-test.

## Writing tests

- Public API: `mock_http.get(f"{MC_API_BASE_URL}/api/v2/recipes/1").respond(json=...)`.
- Authenticated calls are all `POST MC_PROXY_API_URL` with a JSON body
  `{"endpoint", "lang", "method", "payload"?}`. Assert on `route.calls.last.request.content`.
- Sample responses live in `tests/fixtures/` (load with the `load_fixture` fixture).
  New fixtures must be hand-written or anonymised. Never paste real cookies, tokens or
  personal data.
- Tests are `async def` and need no decorator (`asyncio_mode = "auto"`).

## API notes

- Public recipe API (no auth): `https://mc-api.tecpal.com/api/v1/recipes/search/page/{n}`,
  `/api/v1/recipes/filters`, `/api/v2/recipes/{id}`. Category, complexity and
  ingredient-category ids depend on the language, so the same `language` must be used
  for filters and search. The default language is `pl-PL`. Stu is in the UK, but whether
  the API serves `en-GB` hasn't been checked, so don't change the default without a feature request asking for it.
- Authoring targets the Monsieur Cuisine Smart (MC3.0, `deviceTypeId` 13) only.
- The mc-api often returns HTTP 403 for ordinary business errors, with a JSON
  `{"code", "message"}` body. Surface that message instead of assuming an auth failure.
- Reference: Toque (`~/reference/MrCuisineVibeConnect`, read-only, MIT) documents the
  public recipe API in `core/src/main/kotlin/app/toque/core/recipes/McOnline.kt`.
