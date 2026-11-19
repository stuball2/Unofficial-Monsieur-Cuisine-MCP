`mc-online-recipe.json` and `mc-online-page.json` are synthetic mc-api.tecpal.com
response samples copied from Toque (https://github.com/Wuki60/MrCuisineVibeConnect,
`core/src/test/resources/fixtures/`), MIT License, Copyright (c) 2026 Wuki60 and
Toque contributors. They contain made-up data (example.test URLs), not real recipes.

Add new fixtures here as JSON files and load them with the `load_fixture` fixture.
Never record fixtures containing real cookies, tokens or personal data.

`mc3-en-recipe.json` / `mc3-en-search-page.json` are hand-written (made-up content) in the
exact shape returned for Monsieur Cuisine Smart (MC3.0) recipes in `en-US` by
`/api/v2/recipes/{id}` and `/api/v1/recipes/search/page/{n}` when the `device-type: web`
header is sent (without it every ingredient `name` is null). Step machine settings live in
`steps[].deviceSetting` (`mode`, `reverse`, `turbo`, `settings[0]` with speed / temperature
°C / time in seconds / weight g); a step with no machine action has `deviceSetting: null`.
